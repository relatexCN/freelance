import os
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("OLLAMA_API_KEY")

if key:
    print(f"OLLAMA_API_KEY Found: {key[:3]}... (Length: {len(key)})")
else:
    print("OLLAMA_API_KEY Not Found!")
