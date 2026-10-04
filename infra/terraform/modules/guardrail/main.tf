# Bedrock Guardrail on model traffic: masks credentials/financial PII in model
# OUTPUT (the legacy `action` applies to output only - verified with
# ApplyGuardrail: source=INPUT detects nothing), and blocks responses that
# contain private keys, GitHub tokens, symmetric keys or other secret material.

locals {
  # Regex filters are detect-only on input (a legacy file containing a key
  # must not abort the run when the agent reads it) and BLOCK on output (the
  # model must never echo secret material into the chat).
  secret_regexes = [
    {
      name        = "ssh-or-pem-private-key"
      description = "PEM / OpenSSH / PGP private key header."
      pattern     = "-----BEGIN (RSA |OPENSSH |EC |DSA |PGP |ENCRYPTED )?PRIVATE KEY( BLOCK)?-----"
    },
    {
      name        = "github-token"
      description = "GitHub personal access, OAuth, app or fine-grained token."
      pattern     = "\\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\\b|\\bgithub_pat_[A-Za-z0-9_]{60,}\\b"
    },
    {
      name        = "aes-or-symmetric-key-assignment"
      description = "An AES, HMAC, signing or generic secret key assigned a hex or base64 literal."
      pattern     = "(?i)\\b(aes|secret|private|encryption|symmetric|hmac|signing)[_\\-\\s]*key\\s*[:=]\\s*['\"]?([A-Fa-f0-9]{32,64}|[A-Za-z0-9+/]{22,}={0,2})['\"]?"
    },
    {
      name        = "slack-token"
      description = "Slack bot, user or app token."
      pattern     = "\\bxox[baprs]-[A-Za-z0-9-]{10,}\\b"
    },
    {
      name        = "json-web-token"
      description = "Signed JWT."
      pattern     = "\\beyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}\\b"
    },
  ]
}

resource "aws_bedrock_guardrail" "this" {
  name        = var.guardrail_name
  description = "Sensitive-data guardrail for the ${var.project_name} code upgrade agent."

  blocked_input_messaging   = "The request was blocked by the ${var.project_name} guardrail because it contains secret material (a private key, access token or similar). Remove the secret and try again."
  blocked_outputs_messaging = "The response was withheld by the ${var.project_name} guardrail because it would have exposed secret material. Rotate the credential and remove it from the repository before continuing."

  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = var.pii_entity_types
      content {
        type   = pii_entities_config.value
        action = var.pii_action
      }
    }

    dynamic "regexes_config" {
      for_each = local.secret_regexes
      content {
        name          = regexes_config.value.name
        description   = regexes_config.value.description
        pattern       = regexes_config.value.pattern
        action        = "BLOCK"
        input_action  = "NONE"
        output_action = "BLOCK"
      }
    }
  }
}

# A numbered version is what the app references; DRAFT changes on every edit.
# Replace the version whenever the guardrail changes so the app always runs
# the current configuration.
resource "aws_bedrock_guardrail_version" "this" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "Published by Terraform for ${var.project_name}."

  lifecycle {
    replace_triggered_by = [aws_bedrock_guardrail.this]
  }
}

resource "aws_ssm_parameter" "guardrail_id" {
  name        = "${var.parameter_store_prefix}guardrail_id"
  type        = "String"
  value       = aws_bedrock_guardrail.this.guardrail_id
  description = "${var.project_name} Bedrock guardrail id."
}

resource "aws_ssm_parameter" "guardrail_version" {
  name        = "${var.parameter_store_prefix}guardrail_version"
  type        = "String"
  value       = aws_bedrock_guardrail_version.this.version
  description = "${var.project_name} Bedrock guardrail version."
}

# --- repository personal-data scan ----------------------------------------------
# A second guardrail that never sits on model traffic: forge calls ApplyGuardrail
# with it (source=OUTPUT) to find personal data in the cloned repository's data
# files (fixtures, SQL seeds, properties). Broad entity types are fine here because
# nothing is masked in code - the anonymized text is discarded, only the detected
# types and matches (located, then dropped) are used for the report.
resource "aws_bedrock_guardrail" "pii_scan" {
  name        = "${var.guardrail_name}-pii-scan"
  description = "Detects personal data in repository files scanned by ${var.project_name} (report only)."

  blocked_input_messaging   = "Not used: the ${var.project_name} PII scan guardrail only reports."
  blocked_outputs_messaging = "Not used: the ${var.project_name} PII scan guardrail only reports."

  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = var.pii_scan_entity_types
      content {
        type           = pii_entities_config.value
        action         = "ANONYMIZE"
        input_enabled  = false
        output_enabled = true
        output_action  = "ANONYMIZE"
      }
    }
  }
}

resource "aws_bedrock_guardrail_version" "pii_scan" {
  guardrail_arn = aws_bedrock_guardrail.pii_scan.guardrail_arn
  description   = "Published by Terraform for the ${var.project_name} repository PII scan."

  lifecycle {
    replace_triggered_by = [aws_bedrock_guardrail.pii_scan]
  }
}

resource "aws_ssm_parameter" "pii_scan_guardrail_id" {
  name        = "${var.parameter_store_prefix}pii_scan_guardrail_id"
  type        = "String"
  value       = aws_bedrock_guardrail.pii_scan.guardrail_id
  description = "${var.project_name} repository PII scan guardrail id."
}

resource "aws_ssm_parameter" "pii_scan_guardrail_version" {
  name        = "${var.parameter_store_prefix}pii_scan_guardrail_version"
  type        = "String"
  value       = aws_bedrock_guardrail_version.pii_scan.version
  description = "${var.project_name} repository PII scan guardrail version."
}
