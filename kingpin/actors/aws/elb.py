# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright 2014 Nextdoor.com, Inc

"""AWS.ELB Actors"""

import logging
import math

from boto.ec2 import elb as aws_elb
from concurrent import futures
from retrying import retry
from tornado import concurrent
from tornado import gen
from tornado import ioloop

from kingpin import utils
from kingpin.actors.aws import base
from kingpin.actors import exceptions
from kingpin.actors.aws import settings as aws_settings
from kingpin.constants import REQUIRED

log = logging.getLogger(__name__)

__author__ = 'Mikhail Simin <mikhail@nextdoor.com>'


# This executor is used by the tornado.concurrent.run_on_executor()
# decorator. We would like this to be a class variable so its shared
# across RightScale objects, but we see testing IO errors when we
# do this.
EXECUTOR = futures.ThreadPoolExecutor(10)


# Helper function
def p2f(string):
    """Convert percentage string into float.

    Converts string like '78.9%' into 0.789
    """
    return float(string.strip('%')) / 100


class WaitUntilHealthy(base.AWSBaseActor):

    """Waits till a specified number of instances are "InService"."""

    all_options = {
        'name': (str, REQUIRED, 'Name of the ELB'),
        'count': ((int, str), REQUIRED,
                  'Specific count, or percentage of instances to wait for.'),
        'region': (str, REQUIRED, 'AWS region name, like us-west-2')
    }

    # Get references to existing objects that are used by the
    # tornado.concurrent.run_on_executor() decorator.
    ioloop = ioloop.IOLoop.current()
    executor = EXECUTOR

    def __init__(self, *args, **kwargs):
        """Set up connection object.

        Option Arguments:
            name: string - name of the ELB
            count: int, or string with %. (i.e. 4, or '80%')
            region: string - AWS region name, like us-west-2.
        """

        super(WaitUntilHealthy, self).__init__(*args, **kwargs)

        region = self._get_region(self.option('region'))

        if not (aws_settings.AWS_ACCESS_KEY_ID and
                aws_settings.AWS_SECRET_ACCESS_KEY):
            raise exceptions.InvalidCredentials(
                'AWS settings imported but not all credentials are supplied. '
                'AWS_ACCESS_KEY_ID: %s, AWS_SECRET_ACCESS_KEY: %s' % (
                    aws_settings.AWS_ACCESS_KEY_ID,
                    aws_settings.AWS_SECRET_ACCESS_KEY))

        self.conn = aws_elb.ELBConnection(
            aws_settings.AWS_ACCESS_KEY_ID,
            aws_settings.AWS_SECRET_ACCESS_KEY,
            region=region)

    def _get_region(self, region):
        """Return 'region' object used in ELBConnection

        Args:
            region: string - AWS region name, like us-west-2
        Returns:
            RegionInfo object from boto.ec2.elb
        """

        all_regions = aws_elb.regions()
        match = [r for r in all_regions if r.name == region]

        if len(match) != 1:
            raise exceptions.UnrecoverableActorFailure((
                'Expected to find exactly 1 region named %s. '
                'Found: %s') % (region, match))

        return match[0]

    def _get_expected_count(self, count, total_count):
        """Calculate the expected count for a given percentage.

        Either returns the passed count if it's an integer, or
        calculates the count given an expected percentage.

        Args:
            count: Minimum count (int) or percentage (int) of hosts that must
                   be healthy.
            total_count: The total number of instances in the ELB.

        Returns:
            Number of instances required to be 'healthy'
        """

        if '%' in str(count):
            expected_count = math.ceil(total_count * p2f(count))
        else:
            expected_count = int(count)

        return expected_count

    @concurrent.run_on_executor
    @utils.exception_logger
    def _is_healthy(self, elb, count):
        """Check if there are `count` InService instances for a given elb.

        Args:
            count: integer, or string with % in it.
                   for more information read _get_expected_count()

        Returns:
            Boolean whether or not the ELB is healthy enough.
        """
        name = elb.name

        self.log.debug('Counting ELB InService instances for : %s' % name)

        # Get all instances for this ELB
        instance_list = elb.get_instance_health()
        total_count = len(instance_list)

        self.log.debug('All instances: %s' % instance_list)
        in_service_count = [
            i.state for i in instance_list].count('InService')

        expected_count = self._get_expected_count(count, total_count)

        healthy = (in_service_count >= expected_count)
        self.log.debug('ELB "%s" healthy state: %s' % (elb.name, healthy))

        return healthy

    @gen.coroutine
    def _execute(self):
        """Executes an actor and yields the results when its finished.

        raises: gen.Return(True)
        """

        elb = yield self._find_elb(name=self.option('name'))

        repeating_log = utils.create_repeating_log(
            self.log.info,
            'Still waiting for %s to become healthy' % self.option('name'),
            seconds=30)
        while True:
            healthy = yield self._is_healthy(elb, count=self.option('count'))

            if healthy is True:
                self.log.info('ELB is healthy.')
                break

            # In dry mode, fake it
            if self._dry:
                self.log.info('Pretending that ELB is healthy.')
                break

            # Not healthy :( continue looping
            self.log.debug('Retrying in 3 seconds.')
            yield utils.tornado_sleep(3)

        utils.clear_repeating_log(repeating_log)

        raise gen.Return()


class RegisterInstance(base.AWSBaseActor):

    """Add an EC2 instance to a load balancer.

    http://boto.readthedocs.org/en/latest/ref/elb.html
    #boto.ec2.elb.ELBConnection.register_instances
    """

    all_options = {
        'elb': (str, REQUIRED, 'Name of the ELB'),
        'region': (str, REQUIRED, 'AWS region name, like us-west-2'),
        'instance_id': ((str, list), None, (
            'Instance id, or list of ids. If no value is specified then '
            'the instance id of the executing machine is used.'))
    }

    @concurrent.run_on_executor
    @utils.exception_logger
    @retry
    def _add(self, elb, instances):
        """Invoke elb.register_instances

        This boto function is idempotent, so any retry is OK.

        Args:
            elb: boto Loadbalancer object
            instances: list of instance ids.
        """
        elb.register_instances(instances)

    @gen.coroutine
    def _execute(self):
        elb = yield self._find_elb(self.option('elb'))
        instances = self.option('instance_id')
        if type(self.option('instance_id')) is not list:
            instances = [self.option('instance_id')]

        self.log.info(('Adding the following instances to elb: '
                       '%s' % ', '.join(instances)))
        if not self._dry:
            yield self._add(elb, instances)
            self.log.info('Done.')


class DeregisterInstance(base.AWSBaseActor):

    """Remove EC2 instance(s) from an ELB.

    http://boto.readthedocs.org/en/latest/ref/elb.html
    #boto.ec2.elb.loadbalancer.LoadBalancer.deregister_instances
    """

    all_options = {
        'elb': (str, REQUIRED, 'Name of the ELB'),
        'region': (str, REQUIRED, 'AWS region name, like us-west-2'),
        'instance_id': ((str, list), None, (
            'Instance id, or list of ids. If no value is specified then '
            'the instance id of the executing machine is used.'))
    }

    @concurrent.run_on_executor
    @utils.exception_logger
    @retry
    def _remove(self, elb, instances):
        """Invoke elb.deregister_instances

        This boto function is idempotent, so any retry is OK.

        Args:
            elb: boto Loadbalancer object
            instances: list of instance ids.
        """
        elb.deregister_instances(instances)

    @gen.coroutine
    def _execute(self):
        elb = yield self._find_elb(self.option('elb'))
        instances = self.option('instance_id')
        if type(self.option('instance_id')) is not list:
            instances = [self.option('instance_id')]

        self.log.info(('Removing the following instances from elb: '
                       '%s' % ', '.join(instances)))
        if not self._dry:
            yield self._remove(elb, instances)
            self.log.info('Done.')
