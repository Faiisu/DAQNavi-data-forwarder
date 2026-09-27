# Record acquisition gaps explicitly

The system records the start, end, and cause of an interval when production acquisition could not collect samples, including host downtime and a full application buffer when those boundaries can be observed. The web UI and graphs display these gaps so an absent measurement cannot be mistaken for a zero-valued sensor reading. Gaps cannot be filled with mockup samples.
