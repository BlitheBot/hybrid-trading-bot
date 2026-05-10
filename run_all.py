import subprocess
import sys
import time

# Port assignments:
#   8501 — Streamlit dashboard  (Railway public domain routes here)
#   8502 — Flask /health endpoint (internal; bot.py)
DASHBOARD_PORT = "8501"


def run():
    print("🚀 Starting the Dual-Engine Manager (Bot + Dashboard)...")

    # 1. Trading bot (also starts Flask /health on 8502 internally)
    print("🤖 Launching Trading Bot...")
    bot_process = subprocess.Popen(
        [sys.executable, "bot.py"],
        stdout=sys.stdout,
        stderr=sys.stderr,
        bufsize=1,
        universal_newlines=True,
    )

    # Give the bot a moment to initialise before the dashboard starts
    time.sleep(5)

    # 2. Streamlit dashboard — always on 8501 regardless of Railway $PORT
    print(f"📊 Launching Dashboard on port {DASHBOARD_PORT}...")
    dash_process = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", "dashboard.py",
            "--server.port",    DASHBOARD_PORT,
            "--server.address", "0.0.0.0",
            "--server.headless", "true",
        ],
        stdout=sys.stdout,
        stderr=sys.stderr,
        bufsize=1,
        universal_newlines=True,
    )

    print("✅ Both processes are now running.")

    try:
        while True:
            if bot_process.poll() is not None:
                print("⚠️ Trading Bot exited — restarting in 10s...")
                time.sleep(10)
                bot_process = subprocess.Popen(
                    [sys.executable, "bot.py"],
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )

            if dash_process.poll() is not None:
                print("⚠️ Dashboard exited — restarting in 10s...")
                time.sleep(10)
                dash_process = subprocess.Popen(
                    [
                        sys.executable, "-m", "streamlit", "run", "dashboard.py",
                        "--server.port",    DASHBOARD_PORT,
                        "--server.address", "0.0.0.0",
                        "--server.headless", "true",
                    ],
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )

            time.sleep(5)

    except KeyboardInterrupt:
        print("🛑 Shutting down...")
        bot_process.terminate()
        dash_process.terminate()


if __name__ == "__main__":
    run()
