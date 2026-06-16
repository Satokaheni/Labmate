import requests
from config.settings import MEMORY_URL
def recall(query):
    try:
        response = requests.get(f"{MEMORY_URL}/search", params={"q": query}, timeout=2)
        if response.status_code == 200:
            return "\n".join([m['content'] for m in response.json()])
    except Exception: return "Memory server unreachable."
    return "No relevant memories found."
def remember(content):
    try:
        requests.post(f"{MEMORY_URL}/remember", json={"content": content}, timeout=2)
        return "Stored."
    except Exception: return "Failed."
