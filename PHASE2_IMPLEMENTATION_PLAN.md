# Phase 2 Implementation Plan

Scope: durable local single-GPU allocation, concurrency control, and recovery.

1. Extract NVIDIA inventory and compute-process parsing into deterministic,
   hardware-independent functions behind an injectable `NvidiaSmiClient`.
2. Keep physical discovery in `LocalProvider`; overlay active local
   `ComputeJob` reservations in the Controller-side dispatcher without giving
   the provider ORM/database access.
3. Distinguish structural `NO_CAPABLE_RESOURCE` from retryable
   `TEMPORARILY_BUSY`. Defer busy READY Tasks with `not_before`; block only
   structurally unsupported local-only Tasks through `TransitionService`.
4. Persist selected GPU identity and exclusive allocation metadata on
   `ComputeJob`. Treat CREATED/SUBMITTED/PENDING/RUNNING jobs as durable
   reservations and release naturally at COLLECTING/terminal states.
5. Make `prepare()` consume the already selected `local_gpu_N` directly and set
   exactly one `CUDA_VISIBLE_DEVICES`, with no second discovery decision.
6. Prove fake two-GPU concurrency, third-task queueing, external-busy avoidance,
   memory limits, unsupported multi-GPU, CPU independence, cancellation safety,
   CREATED/uncertain-submit boundaries, and restart recovery.
7. Preserve all Phase 1 tests, run an optional read-only real `nvidia-smi`
   inventory check, then update README and implementation status.

Deferred unchanged: multi-controller locking, multi-GPU jobs, fractional GPU,
MIG/MPS, remote/school compute, Agents, Web UI, and migrations.
