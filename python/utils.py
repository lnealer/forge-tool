import logging

import boto3

import settings


def get_logger():
    logging.basicConfig(level=settings.ROOT_LOG_LEVEL)
    # """Configure a logger compatible with local python interpreter and Lambda."""
    logger = logging.getLogger(settings.LOG_NAME)
    logger.setLevel(settings.LOG_LEVEL)
    return logger


def get_config(
    parameter_store_prefix=settings.PARAMETER_STORE_PREFIX,
    parameter_names=settings.SSM_PARAMETER_NAMES,
):
    """Build a config object from parameter store values.

    Withdraw the prefix value from the returned config object.
    """
    ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
    prefixed_parameter_names = [
        f"{parameter_store_prefix}{parameter_name}"
        for parameter_name in parameter_names
    ]
    response = ssm.get_parameters(
        Names=prefixed_parameter_names,
        WithDecryption=True,  # Set to True if any parameters are encrypted
    )

    config = {}
    for param in response.get("Parameters", []):
        config_item_name = param["Name"].split(parameter_store_prefix)[-1]
        config[config_item_name] = param["Value"]

    return config


def get_knowledge_base_id():
    """Resolve the knowledge base id from the env, falling back to SSM.

    infra/deploy.sh writes the id to both .env and the
    ``{prefix}knowledge_base_id`` parameter, so either source works.
    """
    if settings.KNOWLEDGE_BASE_ID:
        return settings.KNOWLEDGE_BASE_ID

    ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
    parameter = ssm.get_parameter(Name=settings.KNOWLEDGE_BASE_ID_PARAMETER)
    return parameter["Parameter"]["Value"]


def get_guardrail_config():
    """Bedrock guardrail config for the chat models, or None if none is set up.

    Resolved from .env first, then the SSM parameters infra/deploy.sh writes.
    A missing guardrail is not fatal: local secret scanning still applies.
    """
    guardrail_id = settings.GUARDRAIL_ID
    version = settings.GUARDRAIL_VERSION
    if not guardrail_id:
        try:
            ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
            response = ssm.get_parameters(
                Names=[settings.GUARDRAIL_ID_PARAMETER, settings.GUARDRAIL_VERSION_PARAMETER]
            )
            values = {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
            guardrail_id = values.get(settings.GUARDRAIL_ID_PARAMETER, "")
            version = values.get(settings.GUARDRAIL_VERSION_PARAMETER, version)
        except Exception as e:
            logging.getLogger(settings.LOG_NAME).warning(
                f"No Bedrock guardrail available ({e}); continuing with local scanning only."
            )
            return None
    if not guardrail_id:
        return None

    config = {"guardrailIdentifier": guardrail_id, "guardrailVersion": version or "DRAFT"}
    if settings.GUARDRAIL_TRACE:
        config["trace"] = "enabled"
    return config
