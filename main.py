from core.orchestrator import LocalClawOrchestrator
if __name__ == "__main__":
    agent = LocalClawOrchestrator()
    print("\nLOCAL-CLAW ACTIVE AGENCY MODE\n")
    while True:
        try:
            user_input = input("You: ")
            if user_input.lower() in ['exit', 'quit']: break
            print(f"Gemma 4: {agent.chat(user_input)}\n")
        except KeyboardInterrupt: break
