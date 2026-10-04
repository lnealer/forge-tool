output "guardrail_id" { value = aws_bedrock_guardrail.this.guardrail_id }
output "guardrail_arn" { value = aws_bedrock_guardrail.this.guardrail_arn }
output "guardrail_version" { value = aws_bedrock_guardrail_version.this.version }
output "pii_scan_guardrail_id" { value = aws_bedrock_guardrail.pii_scan.guardrail_id }
output "pii_scan_guardrail_arn" { value = aws_bedrock_guardrail.pii_scan.guardrail_arn }
output "pii_scan_guardrail_version" { value = aws_bedrock_guardrail_version.pii_scan.version }
