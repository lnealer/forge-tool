output "policy_arn" { value = aws_iam_policy.app.arn }

output "attached_to" {
  value = join(", ", compact([
    var.attach_to_user_name != "" ? "user/${var.attach_to_user_name}" : "",
    var.attach_to_role_name != "" ? "role/${var.attach_to_role_name}" : "",
  ]))
}
