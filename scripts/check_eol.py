"""检查前端文件行尾（CRLF/LF），用绝对路径消除 cwd 歧义。"""
import pathlib
import subprocess

REPO = r"D:\software\口播切片\slicestudio"

files = subprocess.run(
    ["git", "-C", REPO, "ls-files", "frontend"],
    capture_output=True, text=True,
).stdout.splitlines()
sample = [f for f in files if f.endswith((".tsx", ".cjs", ".ts"))][:6]
print(f"index frontend files: {len(files)}")
for f in sample:
    b = (pathlib.Path(REPO) / f).read_bytes()
    print(f, "CRLF" if b"\r\n" in b else "LF")