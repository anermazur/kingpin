##### group.Sync

Groups together a series of Actors and executes them synchronously
in the order that they were defined.

**Options**

  * `acts` - An array of individual Actor definitions.

Examples

    { 'acts': [
      { 'desc': 'sleep', 'actor': 'misc.Sleep',
        'options': { 'sleep': 60 } },
      { 'desc': 'do something', 'actor': 'server_array.Clone',
        'options': { 'source': 'template', 'dest': 'new_array' } },
    ] }

**Dry Mode**

Passes on the Dry mode setting to the sub-actors that are called.