import openai
from fastapi import FastAPI, Request
import re
from openai import OpenAI
import httpx
import asyncio
import os
import requests
import logging
import json
import traceback
from dotenv import load_dotenv
import aiohttp
from aiohttp import BasicAuth

load_dotenv()

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ROUTE = os.getenv("ROUTE")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

print(f"API ключ OpenAI: {OPENAI_API_KEY}")
print(f"URL вебхука: {WEBHOOK_URL}")
print(f"Маршрут: {ROUTE}")
print(f"ID помощника: {ASSISTANT_ID}")

openai.api_key = OPENAI_API_KEY

client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=60, organization="org-MbWjhhShwwmi3Tso0LnLSejk")

user_threads = {}

user_active_runs = {}
tools_list = [
    {
        "type": "file_search"
    }
]

@app.post(ROUTE)
async def process_message(request: Request):
    data = await request.json()
    print(f"Получены данные запроса: {data}")

    client_id = data.get('client_id', '')
    user_key = str(data['client_id'])
    thread_id = user_threads.get(user_key)
    print(f"Обрабатывается сообщение пользователя: {user_key}, thread_id: {thread_id}")

    if not thread_id:
        thread = client.beta.threads.create(
            tool_resources={
                "file_search": {
                    "vector_store_ids": ["vs_DekzgbGTicAnBeIMeArwkvQe"]
                }
            }
        )
        thread_id = thread.id
        user_threads[user_key] = thread_id

    print(f"Создан новый поток: {thread_id}")

    asyncio.create_task(handle_openai_request(data, thread_id, client_id))

    return {
        "id": data['id'],
        "client_id": data['client_id'],
        "chat_id": data['chat_id'],
        "message": {
            "type": "TEXT",
            "text": "Your request is being processed..."
        },
        "event": "BOT_MESSAGE"
    }

async def handle_openai_request(data, thread_id, client_id):
    query = data.get('message', {}).get('text', '')
    if not query:
        return
    
    print(f"Запрос пользователя: {query}")
    user_key = str(data['client_id'])

    while user_key in user_active_runs:
        run_id = user_active_runs[user_key]
        run_status = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run_id
        )

        if run_status.status == 'completed':
            del user_active_runs[user_key]
            break
        elif run_status.status in ['failed', 'rejected']:
            del user_active_runs[user_key]
            answer = "Произошла ошибка при обработке запроса."

        await asyncio.sleep(1)

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=query
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        tools=tools_list
    )
    print(f"Создана новая задача: {run.id}")
    user_active_runs[user_key] = run.id

    while True:
        run_status = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id
        )

        print(f"Статус задачи: {run_status.status}")

        if run_status.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            answer = next((msg.content[0].text.value for msg in messages.data if msg.role == "assistant"), "Обработка запроса...")
            cleaned_answer = await clean_text(answer)
            if user_key in user_active_runs:
                del user_active_runs[user_key]
            break

        elif run_status.status == 'requires_action':
            required_actions = run_status.required_action.submit_tool_outputs.model_dump()
            tool_outputs = []
            for action in required_actions["tool_calls"]:
                func_name = action['function']['name']
                arguments = json.loads(action['function']['arguments'])

                if func_name == "get_order_status_and_tracking":
                    order_id = arguments['order_id']
                    order_info = await get_order_status_and_tracking(order_id)
                    order_info_str = json.dumps(order_info, indent=2)
                    tool_outputs.append({
                        "tool_call_id": action['id'],
                        "output": order_info_str
                    })
                elif func_name == "search_products_by_keyword_and_price":
                    keyword = arguments['keyword']
                    price = arguments.get('price', None)  
                    products = await search_products_by_keyword_and_price(keyword, price)

                    if isinstance(products, list):
                        products_str = json.dumps(products)
                    else:
                        products_str = products
    
                    tool_outputs.append({
                        "tool_call_id": action['id'],
                        "output": products_str 
                    })
                elif func_name == "transfer_to_operator":
                    message_text = arguments['message_text']
                    await transfer_to_operator(data, message_text)
                    tool_outputs.append({
                        "tool_call_id": action['id'],
                        "output": message_text 
                    })
                else:
                    raise ValueError(f"Unknown function: {func_name}")

            client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread_id,
                run_id=run.id,
                tool_outputs=tool_outputs
            )
        elif run_status.status in ['failed', 'rejected']:
            answer = "Произошла ошибка при обработке запроса."
            if user_key in user_active_runs:
                del user_active_runs[user_key]
            break

        await asyncio.sleep(1)

    print(f"Ответ помощника: {cleaned_answer}")

    response_data = {
        "id": data['id'],
        "client_id": data['client_id'],
        "chat_id": data['chat_id'],
        "message": {
            "type": "TEXT",
            "text": cleaned_answer
        },
        "event": "BOT_MESSAGE"
    }

    async with httpx.AsyncClient() as http_client:
        await http_client.post(WEBHOOK_URL, json=response_data)
        print(f"Отправлен ответ на вебхук: {WEBHOOK_URL}")

async def clean_text(text):
    # Удаление ссылок на источники
    text = re.sub(r'【[^】]*】', '', text)
    return text
