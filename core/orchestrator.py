import torch
import re
from unsloth import FastLanguageModel
from config.settings import MODEL_NAME, MAX_SEQ_LENGTH
from core.prompt_manager import SYSTEM_PROMPT
from tools.memory_tool import recall, remember
from tools.code_tool import search_code

class LocalClawOrchestrator:
    def __init__(self):
        self.model, self.tokenizer = FastLanguageModel.from_pretrained(
            model_name=MODEL_NAME, max_seq_length=MAX_SEQ_LENGTH, load_in_4bit=True
        )
        FastLanguageModel.for_inference(self.model)
        self.tools = {"recall_memory": recall, "search_code": search_code}
    def run_tool(self, tool_call):
        match = re.search(r"\[TOOL:\s*(\w+)\((['\"])(.*?)\2\)", tool_call)
        if match:
            name, query = match.group(1), match.group(3)
            if name in self.tools: return self.tools[name](query)
        return "Error: Invalid tool call."
    def chat(self, user_input):
        current_prompt = f"<start_of_turn>user\n{SYSTEM_PROMPT}\n\n{user_input}<end_of_turn>\n<start_of_turn>model\n"
        for _ in range(5):
            inputs = self.tokenizer([current_prompt], return_tensors="pt").to("cuda")
            outputs = self.model.generate(**inputs, max_new_tokens=1024, use_cache=True)
            response = self.tokenizer.batch_decode(outputs)[0].split("<start_of_turn>model\n")[-1].strip()
            if "[TOOL:" in response:
                tool_call = re.search(r"\[TOOL:.*?\]", response).group(0)
                result = self.run_tool(tool_call)
                current_prompt += f"{response}\nObservation: {result}\n<start_of_turn>model\n"
            else:
                remember(f"User: {user_input}\nAssistant: {response}")
                return response
        return "Max iterations reached."
