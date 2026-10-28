variable "project_id" {
  description = "Google Cloud project ID to deploy into."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name (also used to name the service account)."
  type        = string
  default     = "retail-intel-agent"
}

variable "artifact_repo" {
  description = "Artifact Registry Docker repository ID."
  type        = string
  default     = "retail-intel-agent"
}

variable "image_tag" {
  description = "Container image tag to deploy (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "api_key_secret_id" {
  description = "Secret Manager secret ID that stores the Gemini API key."
  type        = string
  default     = "google-api-key"
}

variable "pro_model" {
  description = "Reasoning model for the coordinator, writer and reviewer."
  type        = string
  default     = "gemini-3.5-flash"
}

variable "fast_model" {
  description = "Fast model for data and search specialists."
  type        = string
  default     = "gemini-3.5-flash-lite"
}

variable "min_instances" {
  description = "Minimum Cloud Run instances (0 = scale to zero)."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum Cloud Run instances."
  type        = number
  default     = 3
}

variable "allow_unauthenticated" {
  description = "Grant allUsers run.invoker (public demo). Keep false for internal use."
  type        = bool
  default     = false
}
