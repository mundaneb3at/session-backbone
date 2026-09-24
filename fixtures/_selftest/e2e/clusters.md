# Repeated Instruction Clusters

## Cluster 1

- score: 42.00
- size: 4
- distinct sessions: 3
- normalized signature: a after completes json line machine processing readable summary write

VERBATIM samples:

- session_id: finish-report-workflow

  > Write a machine readable JSON summary line after processing completes.

- session_id: consolidate-storage-rules

  > Write a machine readable JSON summary line after all processing completes.

- session_id: consolidate-storage-rules

  > Always write a machine readable JSON summary line after processing completes.

## Cluster 2

- score: 41.00
- size: 4
- distinct sessions: 3
- normalized signature: always and crashing input malformed records rows skip validate without

VERBATIM samples:

- session_id: finish-report-workflow

  > Always validate input rows and skip malformed records without crashing.

- session_id: finish-report-workflow

  > Always validate every input row and skip malformed records without crashing.

- session_id: consolidate-storage-rules

  > Validate input rows and skip malformed records without ever crashing.

UNCLUSTERED: 33.3%
