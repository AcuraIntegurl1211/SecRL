# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import os

from azure.identity import get_bearer_token_provider, AzureCliCredential
from azure.ai.inference import ChatCompletionsClient
from azure.core.credentials import AzureKeyCredential

# token_provider = get_bearer_token_provider(
#     AzureCliCredential(), "https://cognitiveservices.azure.com/.default"
# )

# BE SURE TO include a "tags": [<model_name>] for each dictionary in the config_list to include the model name
# We will filter out the config list passed in to only include the model that model_name in the tags is equal to qa_gen_model
# config_list = [
#     {
#         "model": "some name",
#         "tags": ["gpt-4o"]
#     },
#     {
#         "model": "some name",
#         "tags": ["gpt-3.5"]
#     }
# ]
# If qa_gen_model = "gpt-4o", the config_list for qa_gen will be only the first dictionary in the config_list
# Similarly in run_exp.py, if you set --model gpt-4o, the config_list for the agent will be only the first dictionary in the config_list


_DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

_COMMON_DEEPSEEK_CONFIG = {
    "api_key": _DEEPSEEK_API_KEY,
    "base_url": "https://api.deepseek.com",
    "max_retries": 0,
    "extra_body": {
        "thinking": {
            "type": "disabled",
        }
    },
}

CONFIG_LIST = []

if _DEEPSEEK_API_KEY:
    CONFIG_LIST = [
        {
            **_COMMON_DEEPSEEK_CONFIG,
            "model": "deepseek-v4-pro",
            "tags": ["deepseek-v4-pro"],
            "timeout": 120.0,
        },
        {
            **_COMMON_DEEPSEEK_CONFIG,
            "model": "deepseek-v4-flash",
            "tags": ["deepseek-v4-flash"],
            "timeout": 60.0,
        },
    ]
else:
    print(
        "Potential Error: DEEPSEEK_API_KEY is not set; "
        "CONFIG_LIST will remain empty."
    )
