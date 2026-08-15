# Repeated Instruction Clusters

## Cluster 1

- score: 42.00
- size: 4
- distinct sessions: 3
- normalized signature: a after completes json line machine processing readable summary write

VERBATIM samples:

- session_id: session-a-real-id

  > Write a machine readable JSON summary line after processing completes.

- session_id: session-b-real-id

  > Write a machine readable JSON summary line after all processing completes.

- session_id: session-b-real-id

  > Always write a machine readable JSON summary line after processing completes.

## Cluster 2

- score: 41.00
- size: 4
- distinct sessions: 3
- normalized signature: always and crashing input malformed records rows skip validate without

VERBATIM samples:

- session_id: session-a-real-id

  > Always validate input rows and skip malformed records without crashing.

- session_id: session-a-real-id

  > Always validate every input row and skip malformed records without crashing.

- session_id: session-b-real-id

  > Validate input rows and skip malformed records without ever crashing.

UNCLUSTERED: 33.3%
