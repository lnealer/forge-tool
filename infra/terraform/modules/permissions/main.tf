# One managed policy with exactly what the app needs at runtime, optionally
# attached to the IAM user or role that runs it.

locals {
  bedrock_prefix = "arn:${var.partition}:bedrock:${var.region}:${var.account_id}"

  guardrail_resources = length(compact(var.guardrail_arns)) > 0 ? compact(var.guardrail_arns) : ["${local.bedrock_prefix}:guardrail/*"]
  kb_resource         = var.knowledge_base_id != "" ? "${local.bedrock_prefix}:knowledge-base/${var.knowledge_base_id}" : "${local.bedrock_prefix}:knowledge-base/*"
}

data "aws_iam_policy_document" "app" {
  # Inference profiles need the profile ARN *and* the underlying foundation
  # model ARNs in every region the profile can route to. The Converse API is
  # authorized by these same two actions.
  statement {
    sid    = "InvokeModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "${local.bedrock_prefix}:inference-profile/${var.primary_model_id}",
      "${local.bedrock_prefix}:inference-profile/${var.reviewer_model_id}",
      "arn:${var.partition}:bedrock:*::foundation-model/${var.primary_base_model_id}",
      "arn:${var.partition}:bedrock:*::foundation-model/${var.reviewer_base_model_id}",
    ]
  }

  statement {
    sid       = "ReadInferenceProfiles"
    effect    = "Allow"
    actions   = ["bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"]
    resources = ["*"]
  }

  statement {
    sid       = "ApplyGuardrail"
    effect    = "Allow"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = local.guardrail_resources
  }

  statement {
    sid       = "QueryKnowledgeBase"
    effect    = "Allow"
    actions   = ["bedrock:Retrieve"]
    resources = [local.kb_resource]
  }

  # SSM: the PAT (SecureString) plus the ids the other modules publish.
  statement {
    sid       = "ReadParameters"
    effect    = "Allow"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:${var.partition}:ssm:${var.region}:${var.account_id}:parameter/${var.parameter_store_prefix}*"]
  }

  statement {
    sid       = "DecryptSecureStrings"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:${var.partition}:kms:${var.region}:${var.account_id}:key/*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

# Region-suffixed for the same reason as the KB role: IAM names are global.
resource "aws_iam_policy" "app" {
  name        = "${var.project_name}-app-policy-${var.region}"
  description = "Runtime permissions for the ${var.project_name} upgrade agent."
  policy      = data.aws_iam_policy_document.app.json
}

resource "aws_iam_user_policy_attachment" "user" {
  count      = var.attach_to_user_name != "" ? 1 : 0
  user       = var.attach_to_user_name
  policy_arn = aws_iam_policy.app.arn
}

resource "aws_iam_role_policy_attachment" "role" {
  count      = var.attach_to_role_name != "" ? 1 : 0
  role       = var.attach_to_role_name
  policy_arn = aws_iam_policy.app.arn
}
