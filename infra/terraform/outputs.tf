# Empty strings when the corresponding part is not created, so deploy.sh can
# always `terraform output -raw` them.

output "knowledge_base_id" {
  value = try(module.knowledge_base[0].knowledge_base_id, "")
}

output "knowledge_base_arn" {
  value = try(module.knowledge_base[0].knowledge_base_arn, "")
}

output "data_source_id" {
  value = try(module.knowledge_base[0].data_source_id, "")
}

output "docs_bucket_name" {
  value = try(module.knowledge_base[0].docs_bucket_name, "")
}

output "vector_index_arn" {
  value = try(module.knowledge_base[0].vector_index_arn, "")
}

output "knowledge_base_role_arn" {
  value = try(module.knowledge_base[0].role_arn, "")
}

output "guardrail_id" {
  value = try(module.guardrail[0].guardrail_id, "")
}

output "guardrail_arn" {
  value = try(module.guardrail[0].guardrail_arn, "")
}

output "guardrail_version" {
  value = try(module.guardrail[0].guardrail_version, "")
}

output "app_policy_arn" {
  value = try(module.permissions[0].policy_arn, "")
}

output "app_policy_attached_to" {
  value = try(module.permissions[0].attached_to, "")
}

output "pii_scan_guardrail_id" {
  value = try(module.guardrail[0].pii_scan_guardrail_id, "")
}

output "pii_scan_guardrail_version" {
  value = try(module.guardrail[0].pii_scan_guardrail_version, "")
}
