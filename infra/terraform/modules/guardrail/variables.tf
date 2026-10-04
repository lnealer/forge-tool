variable "project_name" { type = string }
variable "guardrail_name" { type = string }
variable "pii_action" { type = string }
variable "parameter_store_prefix" { type = string }

# Kept to credentials and financial identifiers on purpose: EMAIL, NAME, URL
# and IP_ADDRESS appear legitimately all over Java projects and masking them
# would corrupt code. Extend here to add more.
variable "pii_entity_types" {
  type = list(string)
  default = [
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "PASSWORD",
    "CREDIT_DEBIT_CARD_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "US_BANK_ACCOUNT_NUMBER",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "PIN",
  ]
}

# Entity types the repository scan reports. Broader than pii_entity_types because
# this guardrail never touches model traffic. IP_ADDRESS, AGE, URL and USERNAME
# are left out: they are everywhere in code and tests (ams NetworkValidation tests
# are full of IP addresses) and would bury the real findings.
variable "pii_scan_entity_types" {
  type = list(string)
  default = [
    "NAME",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "US_SOCIAL_SECURITY_NUMBER",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "US_PASSPORT_NUMBER",
    "DRIVER_ID",
    "CREDIT_DEBIT_CARD_NUMBER",
    "CREDIT_DEBIT_CARD_CVV",
    "CREDIT_DEBIT_CARD_EXPIRY",
    "US_BANK_ACCOUNT_NUMBER",
    "US_BANK_ROUTING_NUMBER",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "SWIFT_CODE",
    "UK_NATIONAL_INSURANCE_NUMBER",
    "CA_SOCIAL_INSURANCE_NUMBER",
    "PIN",
  ]
}
