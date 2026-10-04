# Every variable is fed from the repo-root .env by infra/deploy.sh, which exports
# them as TF_VAR_<name>. Running terraform directly works the same way.

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project_name" {
  type    = string
  default = "forge-tool"
}

variable "parameter_store_prefix" {
  type        = string
  default     = "forge_tool_"
  description = "Prefix of the SSM parameters the app reads (PAT, KB id, guardrail id)."
}

# --- which parts to manage ---------------------------------------------------
# Setting one of these to false removes that part from the deployment on the
# next apply (it is destroyed). To merely skip a part for one run without
# touching it, use deploy.sh's --no-kb / --guardrail-only style flags, which
# use -target instead.

variable "create_knowledge_base" {
  type    = bool
  default = true
}

variable "create_guardrail" {
  type    = bool
  default = true
}

variable "create_permissions" {
  type    = bool
  default = true
}

# --- knowledge base ----------------------------------------------------------

variable "kb_name" {
  type    = string
  default = "forge-tool-knowledge-base"
}

variable "kb_description" {
  type    = string
  default = "Company coding guidelines for J2EE/Spring/Struts upgrades"
}

variable "kb_data_source_name" {
  type    = string
  default = "forge-tool-guidelines"
}

variable "kb_embedding_model_id" {
  type    = string
  default = "amazon.titan-embed-text-v2:0"
}

variable "kb_embedding_dimensions" {
  type        = number
  default     = 1024
  description = "Must be a size the embedding model supports (Titan v2: 1024, 512, 256). Applied to the index and the KB."
}

variable "kb_distance_metric" {
  type    = string
  default = "cosine"
  validation {
    condition     = contains(["cosine", "euclidean"], var.kb_distance_metric)
    error_message = "kb_distance_metric must be cosine or euclidean."
  }
}

variable "kb_chunk_max_tokens" {
  type    = number
  default = 500
}

variable "kb_chunk_overlap_percent" {
  type    = number
  default = 20
}

variable "kb_vector_bucket_name" {
  type        = string
  default     = ""
  description = "Empty = <project>-vectors-<account>-<region>."
}

variable "kb_vector_index_name" {
  type    = string
  default = "forge-tool-kb-index"
}

variable "kb_docs_bucket_name" {
  type        = string
  default     = ""
  description = "Empty = <project>-kb-<account>-<region>."
}

variable "kb_docs_prefix" {
  type    = string
  default = "guidelines/"
}

# --- guardrail ---------------------------------------------------------------

variable "guardrail_name" {
  type    = string
  default = "forge-tool-guardrail"
}

variable "guardrail_pii_action" {
  type        = string
  default     = "ANONYMIZE"
  description = "ANONYMIZE masks detected PII values with tags; BLOCK rejects the request/response."
  validation {
    condition     = contains(["ANONYMIZE", "BLOCK"], var.guardrail_pii_action)
    error_message = "guardrail_pii_action must be ANONYMIZE or BLOCK."
  }
}

# --- runtime permissions -----------------------------------------------------

variable "app_iam_principal" {
  type        = string
  default     = "auto"
  description = "auto (the identity running terraform), user:<name>, role:<name>, or none."
  validation {
    condition     = var.app_iam_principal == "auto" || var.app_iam_principal == "none" || can(regex("^(user|role):.+", var.app_iam_principal))
    error_message = "app_iam_principal must be auto, none, user:<name> or role:<name>."
  }
}

variable "bedrock_model_id" {
  type        = string
  default     = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
  description = "Inference profile / model id used by the upgrade agent."
}

variable "primary_base_model_id" {
  type        = string
  default     = "anthropic.claude-haiku-4-5-20251001-v1:0"
  description = "Foundation model behind bedrock_model_id; a cross-region profile routes to it in several regions."
}

variable "reviewer_model_id" {
  type    = string
  default = "us.amazon.nova-pro-v1:0"
}

variable "reviewer_base_model_id" {
  type    = string
  default = "amazon.nova-pro-v1:0"
}
