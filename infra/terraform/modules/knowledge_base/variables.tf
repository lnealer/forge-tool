variable "project_name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "partition" { type = string }
variable "parameter_store_prefix" { type = string }

variable "kb_name" { type = string }
variable "kb_description" { type = string }
variable "data_source_name" { type = string }
variable "embedding_model_id" { type = string }
variable "embedding_dimensions" { type = number }
variable "distance_metric" { type = string }
variable "chunk_max_tokens" { type = number }
variable "chunk_overlap_percent" { type = number }
variable "docs_bucket_name" { type = string }
variable "docs_prefix" { type = string }
variable "vector_bucket_name" { type = string }
variable "vector_index_name" { type = string }
