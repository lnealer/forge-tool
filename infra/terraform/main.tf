data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  caller_arn = data.aws_caller_identity.current.arn

  docs_bucket_name   = var.kb_docs_bucket_name != "" ? var.kb_docs_bucket_name : "${var.project_name}-kb-${local.account_id}-${var.aws_region}"
  vector_bucket_name = var.kb_vector_bucket_name != "" ? var.kb_vector_bucket_name : "${var.project_name}-vectors-${local.account_id}-${var.aws_region}"

  # Who gets the runtime policy. "auto" derives the user or role from the
  # identity running terraform: arn:...:user/NAME or arn:...:assumed-role/ROLE/SESSION.
  auto_user = can(regex(":user/(.+)$", local.caller_arn)) ? regex(":user/(.+)$", local.caller_arn)[0] : ""
  auto_role = can(regex(":assumed-role/([^/]+)/", local.caller_arn)) ? regex(":assumed-role/([^/]+)/", local.caller_arn)[0] : ""

  attach_user = (
    var.app_iam_principal == "auto" ? local.auto_user :
    startswith(var.app_iam_principal, "user:") ? trimprefix(var.app_iam_principal, "user:") : ""
  )
  attach_role = (
    var.app_iam_principal == "auto" ? local.auto_role :
    startswith(var.app_iam_principal, "role:") ? trimprefix(var.app_iam_principal, "role:") : ""
  )
}

module "guardrail" {
  count  = var.create_guardrail ? 1 : 0
  source = "./modules/guardrail"

  project_name           = var.project_name
  guardrail_name         = var.guardrail_name
  pii_action             = var.guardrail_pii_action
  parameter_store_prefix = var.parameter_store_prefix
}

module "knowledge_base" {
  count  = var.create_knowledge_base ? 1 : 0
  source = "./modules/knowledge_base"

  project_name           = var.project_name
  region                 = var.aws_region
  account_id             = local.account_id
  partition              = local.partition
  parameter_store_prefix = var.parameter_store_prefix

  kb_name               = var.kb_name
  kb_description        = var.kb_description
  data_source_name      = var.kb_data_source_name
  embedding_model_id    = var.kb_embedding_model_id
  embedding_dimensions  = var.kb_embedding_dimensions
  distance_metric       = var.kb_distance_metric
  chunk_max_tokens      = var.kb_chunk_max_tokens
  chunk_overlap_percent = var.kb_chunk_overlap_percent
  docs_bucket_name      = local.docs_bucket_name
  docs_prefix           = var.kb_docs_prefix
  vector_bucket_name    = local.vector_bucket_name
  vector_index_name     = var.kb_vector_index_name
}

module "permissions" {
  count  = var.create_permissions ? 1 : 0
  source = "./modules/permissions"

  project_name           = var.project_name
  region                 = var.aws_region
  account_id             = local.account_id
  partition              = local.partition
  parameter_store_prefix = var.parameter_store_prefix

  primary_model_id       = var.bedrock_model_id
  primary_base_model_id  = var.primary_base_model_id
  reviewer_model_id      = var.reviewer_model_id
  reviewer_base_model_id = var.reviewer_base_model_id

  # Scope narrows automatically to the guardrail / KB when they are managed here.
  guardrail_arns = compact([
    try(module.guardrail[0].guardrail_arn, ""),
    try(module.guardrail[0].pii_scan_guardrail_arn, ""),
  ])
  knowledge_base_id = try(module.knowledge_base[0].knowledge_base_id, "")

  attach_to_user_name = local.attach_user
  attach_to_role_name = local.attach_role
}
