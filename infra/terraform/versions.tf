terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
  }

  # State lives next to this file (terraform.tfstate, git-ignored). For a shared
  # backend, uncomment, fill in, and run `terraform init -migrate-state`:
  # backend "s3" {
  #   bucket       = "my-terraform-state"
  #   key          = "forge-tool/terraform.tfstate"
  #   region       = "us-east-1"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}
