#!/usr/bin/env bash
# Deploys the agent to Google Cloud Run using the ADK CLI, with
# OpenTelemetry traces exported to Google Cloud Trace.
#
# For a fully reproducible, least-privilege setup (service account, Secret
# Manager, Artifact Registry) use the Terraform in ../terraform instead.
#
# Prerequisites:
#   gcloud auth login && gcloud config set project YOUR_PROJECT_ID
#   export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT to your GCP project id}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE="${SERVICE_NAME:-retail-intel-agent}"

adk deploy cloud_run \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${REGION}" \
  --service_name "${SERVICE}" \
  --app_name retail_intel_agent \
  --with_ui \
  --trace_to_cloud \
  ./retail_intel_agent

# The API key should come from Secret Manager, not a plain env var:
#   gcloud run services update "${SERVICE}" --region "${REGION}" \
#     --update-secrets GOOGLE_API_KEY=google-api-key:latest
