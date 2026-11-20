"""Interactive local image generator; no site or worker queue required."""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE = Path(__file__).resolve().parent
PROJECT_ID = "c5ace0b7-be32-4f78-bf95-bfc3c8f415af"
ASPECTS = ("16:9", "4:3", "1:1", "3:4", "9:16")


def generate(prompt: str, aspect: str, count: int) -> int:
    if not prompt.strip():
        print("Запрос пустой.")
        return 2
    output = BASE / "local_out" / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("GFLOW_CLI_HOME", str(BASE / "gflow_home"))
    env.setdefault("GFLOW_CLI_FLOW_HOST", "flow.google.com")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = [sys.executable, str(BASE / "gflow_ext.py"), "image", "t2i",
           "--model", "nano-pro", "--aspect", aspect, "-n", str(count),
           "--project", PROJECT_ID, "--out", str(output), "--", prompt]
    print(f"\nГенерирую {count} изображение(я). Папка: {output}\n", flush=True)
    result = subprocess.run(cmd, cwd=BASE, env=env)
    if result.returncode == 0:
        images = sorted(p for p in output.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
        print(f"\nГотово: {len(images)} файл(ов) в {output}")
        if sys.platform == "darwin":
            subprocess.run(["open", str(output)], check=False)
    else:
        print(f"\nОшибка генерации (код {result.returncode}).")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальная генерация через Google Flow")
    parser.add_argument("prompt", nargs="?", help="текст запроса; без него откроется интерактивный режим")
    parser.add_argument("--aspect", choices=ASPECTS, default="1:1", help="соотношение сторон")
    parser.add_argument("--count", type=int, choices=range(1, 5), metavar="1-4", default=1)
    args = parser.parse_args()
    if args.prompt is not None:
        return generate(args.prompt, args.aspect, args.count)
    print("Локальная генерация Google Flow. Введите запрос или пустую строку для выхода.")
    while True:
        try:
            prompt = input("\nЗапрос: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            return 0
        generate(prompt, args.aspect, args.count)


if __name__ == "__main__":
    raise SystemExit(main())
