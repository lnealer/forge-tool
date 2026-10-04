# Bedrock knowledge base backed by Amazon S3 Vectors: the documents bucket, the
# vector bucket + index, the service role Bedrock assumes, the knowledge base
# and its S3 data source, and the SSM parameter that publishes the id.
#
# Both buckets use force_destroy so `terraform destroy` removes them cleanly
# (the docs are a copy of knowledge-base/ and the vectors are regenerated on
# re-ingest). Retaining them only ever caused the next create to collide.

locals {
  embedding_model_arn = "arn:${var.partition}:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"
}

# --- guideline documents ------------------------------------------------------

resource "aws_s3_bucket" "docs" {
  bucket        = var.docs_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "docs" {
  bucket = aws_s3_bucket.docs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "docs" {
  bucket = aws_s3_bucket.docs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "docs" {
  bucket                  = aws_s3_bucket.docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- embeddings: S3 Vectors (per-request billing, no capacity floor) ---------

resource "aws_s3vectors_vector_bucket" "vectors" {
  vector_bucket_name = var.vector_bucket_name
  force_destroy      = true
}

resource "aws_s3vectors_index" "kb" {
  vector_bucket_name = aws_s3vectors_vector_bucket.vectors.vector_bucket_name
  index_name         = var.vector_index_name
  data_type          = "float32"
  dimension          = var.embedding_dimensions
  distance_metric    = var.distance_metric

  # Bedrock stores each chunk's text as metadata; it is retrieved, never
  # filtered on, so keep it out of the filterable-metadata size budget.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT"]
  }
}

# --- service role Bedrock assumes --------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${var.partition}:bedrock:${var.region}:${var.account_id}:knowledge-base/*"]
    }
  }
}

# IAM names are account-global; the region suffix lets one deployment per
# region coexist.
resource "aws_iam_role" "kb" {
  name               = "${var.project_name}-kb-role-${var.region}"
  description        = "Service role for the ${var.project_name} Bedrock knowledge base."
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "kb" {
  statement {
    sid       = "InvokeEmbeddingModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.embedding_model_arn]
  }

  statement {
    sid       = "ReadKnowledgeBaseDocuments"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetObject"]
    resources = [aws_s3_bucket.docs.arn, "${aws_s3_bucket.docs.arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [var.account_id]
    }
  }

  statement {
    sid    = "WriteAndQueryVectors"
    effect = "Allow"
    actions = [
      "s3vectors:GetIndex",
      "s3vectors:QueryVectors",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:ListVectors",
      "s3vectors:DeleteVectors",
    ]
    resources = [aws_s3vectors_index.kb.index_arn]
  }

  statement {
    sid       = "DescribeVectorBucket"
    effect    = "Allow"
    actions   = ["s3vectors:GetVectorBucket"]
    resources = [aws_s3vectors_vector_bucket.vectors.vector_bucket_arn]
  }
}

resource "aws_iam_role_policy" "kb" {
  name   = "knowledge-base-access"
  role   = aws_iam_role.kb.id
  policy = data.aws_iam_policy_document.kb.json
}

# Bedrock validates it can assume the role at create time; IAM is eventually
# consistent, so give the new role a moment before the KB is created.
resource "time_sleep" "iam_propagation" {
  depends_on      = [aws_iam_role_policy.kb]
  create_duration = "15s"
}

# --- knowledge base + data source --------------------------------------------

resource "aws_bedrockagent_knowledge_base" "this" {
  name        = var.kb_name
  description = var.kb_description
  role_arn    = aws_iam_role.kb.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = var.embedding_dimensions
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb.index_arn
    }
  }

  depends_on = [time_sleep.iam_propagation]
}

resource "aws_bedrockagent_data_source" "docs" {
  knowledge_base_id    = aws_bedrockagent_knowledge_base.this.id
  name                 = var.data_source_name
  description          = "Coding guideline documents synced from S3."
  data_deletion_policy = "RETAIN"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.docs.arn
      inclusion_prefixes = [var.docs_prefix]
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "FIXED_SIZE"
      fixed_size_chunking_configuration {
        max_tokens         = var.chunk_max_tokens
        overlap_percentage = var.chunk_overlap_percent
      }
    }
  }
}

# Published so the app can resolve the id without editing .env on every host.
resource "aws_ssm_parameter" "kb_id" {
  name        = "${var.parameter_store_prefix}knowledge_base_id"
  type        = "String"
  value       = aws_bedrockagent_knowledge_base.this.id
  description = "${var.project_name} Bedrock knowledge base id."
}
