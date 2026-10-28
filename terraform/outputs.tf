output "service_url" {
  description = "HTTPS URL of the Cloud Run service."
  value       = google_cloud_run_v2_service.agent.uri
}

output "service_account_email" {
  description = "Runtime service account used by the agent."
  value       = google_service_account.agent.email
}

output "image" {
  description = "Container image the service is configured to run."
  value       = local.image
}

output "api_key_secret" {
  description = "Secret Manager secret holding the Gemini API key (add a version out-of-band)."
  value       = google_secret_manager_secret.google_api_key.id
}
