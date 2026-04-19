import subprocess
import time
import sys
import os

def run():
    print("🚀 Starting the Dual-Engine Manager (Bot + Dashboard)...")
    
    # 1. Start the Trading Bot
    print("🤖 Launching Trading Bot...")
    bot_process = subprocess.Popen(
        [sys.executable, "bot.py"],
        stdout=sys.stdout,
        stderr=sys.stderr,
        bufsize=1,
        universal_newlines=True
    )
    
    # Wait a few seconds to let the bot initialize and log account details
    time.sleep(5)
    
    # 2. Start the Streamlit Dashboard
    print("📊 Launching Dashboard...")
    port = os.environ.get("PORT", "8501")
    dash_process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "dashboard.py", "--server.port", port, "--server.address", "0.0.0.0"],
        stdout=sys.stdout,
        stderr=sys.stderr,
        bufsize=1,
        universal_newlines=True
    )
    
    print("✅ Both processes are now running.")
    
    try:
        # Keep the manager script alive while processes are running
        while True:
            if bot_process.poll() is not None:
                print("⚠️ Trading Bot process exited. Restarting in 10 seconds...")
                time.sleep(10)
                bot_process = subprocess.Popen([sys.executable, "bot.py"], stdout=sys.stdout, stderr=sys.stderr)
            
            if dash_process.poll() is not None:
                print("⚠️ Dashboard process exited. Restarting in 10 seconds...")
                time.sleep(10)
                dash_process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "dashboard.py", "--server.port", port], stdout=sys.stdout, stderr=sys.stderr)
                
            time.sleep(5)
    except KeyboardInterrupt:
        print("🛑 Shutting down...")
        bot_process.terminate()
        dash_process.terminate()

if __name__ == "__main__":
    run()
