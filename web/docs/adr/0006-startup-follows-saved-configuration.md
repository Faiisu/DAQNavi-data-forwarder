# Start production acquisition after reboot according to saved configuration

The latest saved web configuration controls whether production acquisition starts after a reboot. When automatic start is enabled, acquisition starts after reboot even if an operator pressed Stop during the previous run; when disabled, it remains stopped. This makes startup behavior explicit in one configuration source rather than depending on transient process state or a separate desired-state file.
