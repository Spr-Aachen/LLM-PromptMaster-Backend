import requests
import json
import json_repair
import requests.adapters
from typing import Optional, Union

##############################################################################################################################

chatURLs_Norm = {
    'gpt-35-turbo': "api/azure/openai/chatCompletion?deploymentId=gpt-35-turbo",
    'gpt-4o': "api/azure/openai/chatCompletion?deploymentId=gpt-4o",
    'gemini-1.5-pro-001': "api/azure/openai/chatCompletion?deploymentId=gemini-1.5-pro-001",
    'moonshot-v1-128k': "api/azure/openai/chatCompletion?deploymentId=moonshot-v1-128k",
    'claude-3-5-sonnet@20240620': "api/azure/openai/chatCompletion?deploymentId=claude-3-5-sonnet@20240620",
}

chatURLs_Paint = {
    'dall-e2': "api/azure/openai/generationImage",
    'dall-e3': "api/azure/openai/generateDell3Image?deploymentId=Dalle3",
}

chatURLs = {**chatURLs_Norm, **chatURLs_Paint}


def gptRequest(
    pfGateway: str = ...,
    gptGateway: str = ...,
    appID: Optional[str] = None,
    appSecret: str = ...,
    model: Optional[str] = None,
    messages: list = [{}],
    options: Optional[dict] = None,
    stream: bool = True,
    **kwargs
):
    # 初始化会话
    session = requests.session()
    session.keep_alive = False
    session.mount('http://', requests.adapters.HTTPAdapter(max_retries = 3))
    session.mount('https://', requests.adapters.HTTPAdapter(max_retries = 3))
    # 获取令牌
    url = f"{pfGateway}/service-pf-open-gateway/oauth/token?grant_type=client_credentials&client_id={appID}&client_secret={appSecret}"
    response = requests.get(
        url = url
    )
    if response.status_code == 200:
        res_token = response.json()
        accessToken = res_token.get("data", {}).get("access_token", "")
        oAuth_token = f"Bearer {accessToken}"
    else:
        yield "Request failed", response.status_code
    # 请求GPT接口
    model = model or list(chatURLs.keys())[0]
    url = f"{gptGateway}/{chatURLs[model]}"
    Headers = {
        'Content-Type': 'application/json',
        'Authorization': oAuth_token
    }
    if model in chatURLs_Norm:
        Payload = {
            'messages': messages,
        }
    if model in chatURLs_Paint:
        Payload = {
            'prompt': f"{messages[0]['content']}\n{messages[1]['content']}",
        }
        stream = False # 图片生成接口不支持流式输出
    Payload = {
        **Payload,
        **(options if options is not None else {})
    }
    try:
        response = requests.post(
            url = url if not stream else url.replace('/chatCompletion', '/streamChatCompletion'),
            headers = Headers,
            data = json.dumps(Payload),
            stream = stream
        )
    except Exception as e:
        yield str(e), 500
    else:
        if response.status_code == 200:
            for chunk in response.iter_content(chunk_size = 1024 if stream else None, decode_unicode = False):
                if chunk:
                    buffer = chunk.decode('utf-8', errors = 'ignore')
                    if stream:
                        buffer = "".join(line[len("data:"):] if line.startswith("data:") else line for line in buffer.splitlines()) # remove all the 'data:' suffix
                    try:
                        parsed_content = json_repair.loads(buffer)
                        #print('buffer successfully parsed:\n', buffer)
                        if model in chatURLs_Norm:
                            result = parsed_content['choices'][0]['delta']['content'] if stream else parsed_content['data']['choices'][0]['message']['content']
                        if model in chatURLs_Paint:
                            result = parsed_content['data']['data'][0]['url']
                        yield result, response.status_code
                    except:
                        #print('failed to load buffer:\n', buffer)
                        continue
        else:
            yield "Request failed", response.status_code

##############################################################################################################################

def assistantRequest(
    pfGateway: str = ...,
    appID: Optional[str] = None,
    appSecret: str = ...,
    chatURL: str = ...,
    xheaderTenant: str = ...,
    assistantCode: str = ...,
    messages: list = [{}],
    options: Optional[dict] = None,
    stream: bool = True,
    **kwargs
):
    # 初始化会话
    session = requests.session()
    session.keep_alive = False
    session.mount('http://', requests.adapters.HTTPAdapter(max_retries = 3))
    session.mount('https://', requests.adapters.HTTPAdapter(max_retries = 3))
    # 获取令牌
    url = f"{pfGateway}/service-pf-open-gateway/oauth/token?grant_type=client_credentials&client_id={appID}&client_secret={appSecret}"
    response = requests.get(
        url = url
    )
    if response.status_code == 200:
        res_token = response.json()
        accessToken = res_token.get("data", {}).get("access_token", "")
        oauth_token = f"Bearer {accessToken}"
    else:
        yield "Request failed", response.status_code
    # 请求智库接口
    url = f"{pfGateway}/{chatURL}/{assistantCode}"
    Headers = {
        'Content-Type': 'application/json',
        'x-header-tenant': xheaderTenant,
        'Authorization': oauth_token
    }
    Payload = {
        'messages': messages,
        **({'options': options} if options is not None else {})
    }
    try:
        response = requests.post(
            url = url if not stream else url.replace('/chat', '/streamChat'),
            headers = Headers,
            data = json.dumps(Payload),
            stream = stream
        )
    except Exception as e:
        yield str(e), 500
    else:
        if response.status_code == 200:
            for chunk in response.iter_content(chunk_size = 1024 if stream else None, decode_unicode = False):
                if chunk:
                    buffer = chunk.decode('utf-8', errors = 'ignore')
                    if stream:
                        buffer = "".join(line[len("data:"):] if line.startswith("data:") else line for line in buffer.splitlines()) # remove all the 'data:' suffix
                    try:
                        parsed_content = json_repair.loads(buffer)
                        #print('buffer successfully parsed:\n', buffer)
                        try:
                            result = parsed_content['dataObject']['choices'][0]['delta']['content'] if stream else parsed_content['data']['data']['choices'][0]['message']['content']
                        except:
                            result = parsed_content['dataContent'] if stream else parsed_content['data']['dataContent']
                        yield result, response.status_code
                    except:
                        #print('failed to load buffer:\n', buffer)
                        continue
        else:
            yield "Request failed", response.status_code

##############################################################################################################################