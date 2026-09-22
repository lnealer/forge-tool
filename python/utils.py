import logging
import boto3


def get_logger():
    # """Configure a logger compatible with local python interpreter and Lambda."""
    logger = logging.getLogger("FORGE_TOOL")
    return logger


def get_config(parameter_store_prefix, parameter_names):
    """Build a config object from parameter store values.

    Withdraw the prefix value from the returned config object.
    """
    ssm = boto3.client("ssm")
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