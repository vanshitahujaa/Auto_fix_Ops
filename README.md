# AutoFixOps
**An Evidence-Grounded, Policy-Safe Self-Healing Framework for Kubernetes Incident Response**

Welcome to the AutoFixOps repository. 

## Documentation
Please refer to the [Technical Design Document](TECHNICAL_DESIGN.md) for a comprehensive overview of the system architecture, component breakdown, technology stack, and evaluation roadmap.

## Project Structure
- `api/` - FastAPI gateway for incident webhooks.
- `workers/` - Celery asynchronous workers for gathering telemetry context and verifying recovery.
- `engine/` - AI-Assisted Diagnosis Engine and Policy Decision logic.
- `kubernetes_integration/` - Kubernetes native Python clients for performing remediation actions.
- `tests/` - Unit, integration, and chaos testing scenarios.
