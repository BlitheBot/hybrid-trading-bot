import subprocess
import sys
import os
import time

def run_bot():
    """Starts the trading bot in the background."""
    print("🚀 Starting Trading Bot...")
    return subprocess.Popen([sys.executable, "bot.py"])

def run_dashboard():
    """Starts the Streamlit dashboard."""
    print("📊 Starting Dashboard...")
    port = os.getenv("PORT", "8501")
    return subprocess.Popen([
        "streamlit", "run", "dashboard.py",
        "--server.port", port,
        "--server.address", "0.0.0.0"
    ])

if __name__ == "__main__":
    bot_process = run_bot()
    dashboard_process = run_dashboard()

    try:
        while True:
            # Check if processes are still running
            if bot_process.poll() is not None:
                print("⚠️ Trading Bot stopped. Restarting...")
                bot_process = run_bot()
            
            if dashboard_process.poll() is not None:
                print("⚠️ Dashboard stopped. Restarting...")
                dashboard_process = run_dashboard()
                
            time.sleep(10)
    except KeyboardInterrupt:
        print("🛑 Stopping all processes...")
        bot_process.terminate()
        dashboard_process.terminate()
