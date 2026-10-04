variable "project_name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "partition" { type = string }
variable "parameter_store_prefix" { type = string }

variable "primary_model_id" { type = string }
variable "primary_base_model_id" { type = string }
variable "reviewer_model_id" { type = string }
variable "reviewer_base_model_id" { type = string }

variable "guardrail_arns" {
  type        = list(string)
  default     = []
  description = "Scope bedrock:ApplyGuardrail to these guardrails (model + PII scan). Empty = any in the account/region."
}

variable "knowledge_base_id" {
  type        = string
  default     = ""
  description = "Scope bedrock:Retrieve to this knowledge base. Empty = any in the account/region."
}

variable "attach_to_user_name" {
  type    = string
  default = ""
}

variable "attach_to_role_name" {
  type    = string
  default = ""
}
