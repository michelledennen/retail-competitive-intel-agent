# Infrastructure as Code (Terraform)

This folder provisions everything the agent needs on Google Cloud:

| Resource | Purpose |
|---|---|
| `google_project_service.apis` | Enables Cloud Run, Artifact Registry, Cloud Build, Secret Manager, Vertex AI, Cloud Trace, Logging, IAM |
| `google_artifact_registry_repository.agent` | Docker repository for the agent image |
| `google_service_account.agent` | Dedicated runtime identity |
| `google_project_iam_member.agent_roles` | Least-privilege roles: `aiplatform.user`, `cloudtrace.agent`, `logging.logWriter`, `monitoring.metricWriter` |
| `google_secret_manager_secret.google_api_key` | Holds the Gemini API key |
| `google_secret_manager_secret_iam_member.agent_access` | `secretmanager.secretAccessor` on **that secret only** |
| `google_cloud_run_v2_service.agent` | Runs the container; `GOOGLE_API_KEY` is injected from Secret Manager |

## Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init
terraform apply -target=google_project_service.apis -target=google_artifact_registry_repository.agent

# Build and push the image (from the repo root)
gcloud builds submit .. --tag us-central1-docker.pkg.dev/$PROJECT/retail-intel-agent/retail-intel-agent:latest

# Add the API key as a secret version (never put it in Terraform files or state)
printf '%s' "$GOOGLE_API_KEY" | gcloud secrets versions add google-api-key --data-file=-

terraform apply        # creates the service account, IAM and Cloud Run service
terraform output service_url
```

The secret **value** is never managed by Terraform, so it never lands in
Terraform state. CI runs `terraform fmt -check` and `terraform validate` on
every push.

## Alternatives

- `make deploy` / `adk deploy cloud_run --trace_to_cloud` for a one-command deploy.
- [Agent Starter Pack](https://github.com/GoogleCloudPlatform/agent-starter-pack)
  for a production template with CI/CD pipelines and Agent Engine support.
