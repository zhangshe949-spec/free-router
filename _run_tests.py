import subprocess, sys
PY = r"C:\Users\qin\AppData\Local\Python\pythoncore-3.14-64\python.exe"
r = subprocess.run([PY, "-m", "pytest", "-q", "--rootdir", "."],
                   cwd=r"L:\free-router",
                   capture_output=True, text=True, timeout=180)
print("RC:", r.returncode)
print("---STDOUT---")
print(r.stdout[-4000:])
print("---STDERR---")
print(r.stderr[-2000:])