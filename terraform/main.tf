terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30, < 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agent.repository_id}/${var.service_name}:${var.image_tag}"

  required_apis = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "iam.googleapis.com",
  ]

  # Least privilege: only what the agent needs at runtime.
  # aiplatform.user: Gemini via Vertex AI; cloudtrace.agent: export spans;
  # logging.logWriter: structured logs; monitoring.metricWriter: metrics.
  # secretmanager.secretAccessor is granted on the API-key secret only (below).
  runtime_roles = [
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------
resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Container registry
# ---------------------------------------------------------------------------
resource "google_artifact_registry_repository" "agent" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repo
  description   = "Container images for the Retail Competitive Intelligence Agent"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Runtime identity (dedicated service account, least privilege)
# ---------------------------------------------------------------------------
resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = "${var.service_name}-sa"
  display_name = "Retail Intel Agent runtime"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(local.runtime_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# ---------------------------------------------------------------------------
# Secret Manager: Gemini API key (value is added out-of-band, never in TF state)
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "google_api_key" {
  project   = var.project_id
  secret_id = var.api_key_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# secretAccessor is granted on THIS secret only, not project-wide.
resource "google_secret_manager_secret_iam_member" "agent_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.google_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run v2 service
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "agent" {
  project             = var.project_id
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.agent.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = local.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "FALSE"
      }
      env {
        name  = "PRO_MODEL"
        value = var.pro_model
      }
      env {
        name  = "FAST_MODEL"
        value = var.fast_model
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }

      # The API key is injected from Secret Manager at runtime; it never appears
      # in the image, the Terraform config or the service definition.
      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.google_api_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.agent_access,
  ]
}

# Optional: allow unauthenticated access (demo only). Keep false for internal use.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.allow_unauthenticated ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
