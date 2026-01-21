import sublime
import sublime_plugin

import os
import re
import time
import threading
import subprocess
from pathlib import Path


# TODO
# - [X] 하단 패널에 결과 출력
# - [X] 실행시 입력 파일을 파이프로 연결
# - [ ] 에러 나더라도 실시간으로 패널에 결과 출력
# - [ ] 문법 에러 발생시 에디터에 표시
# - [X] 프로그램 입력 있으면 무한 반복함


# DOCS: https://www.sublimetext.com/docs/build_systems.html#advanced-example
# DOCS: https://docs.sublimetext.io/reference/commands.html#append
# DOCS: https://www.sublimetext.com/docs/api_reference.html#sublime.Window


class JavaBuilder(sublime_plugin.WindowCommand):
  output_panel = None
  project = None
  process = None

  '''
   [mode]
   - all
   - build
   - check
   - build and run
   - run
   - clean
  '''
  def run(self, mode="build", kill=False, java_home=""):
    if kill or self.process is not None:
      if (self.process is not None) and (self.process.poll() is None):
        self.process.terminate()
        print("[JavaBuilder] Kill")

    if kill:
      return  # When new command, continue the build

    print("[JavaBuilder] Welcome!")

    vars = self.window.extract_variables()

    panel_views = self.window.create_io_panel("exec", on_input=self.on_input_from_panel)
    self.window.run_command("show_panel", {"panel": "output.exec"})
    self.output_panel = panel_views[0]

    self.project = JavaProject()
    self.project.setup_java_cli(java_home)
    self.project.load_project(vars["file"])

    if mode == "all":
      # build
      output = self.project.build()
      self.__append_output_panel__(output)
      if self.project.is_builded:
        # run & clean
        self.process = self.project.run()
        threading.Thread(target=self.run_anyway_with_panel, args=(True,)).start()
      else:
        self.__set_build_messages__(output)
    elif mode == "build":
      output = self.project.build()
      self.__append_output_panel__(output)
      if not self.project.is_builded:
        self.__set_build_messages__(output)
    elif mode == "check":
      output = self.project.build_check()
      self.__append_output_panel__(output)
      if not self.project.is_builded:
        self.__set_build_messages__(output)
    elif mode == "build and run":
      # build
      output = self.project.build()
      self.__append_output_panel__(output)
      if self.project.is_builded:
        # run
        self.process = self.project.run()
        threading.Thread(target=self.run_anyway_with_panel).start()
      else:
        self.__set_build_messages__(output)
    elif mode == "run":
      self.process = self.project.run()
      threading.Thread(target=self.run_anyway_with_panel).start()
    elif mode == "clean":
      output = self.project.clean()
      self.__append_output_panel__(output)
    else:
      message = "[JavaBuilder] Wrong build mode (all, build, check, build and run, run, clean)"
      print(message)
      self.__append_output_panel__(message)

  def on_input_from_panel(self, data):
    # print(f">> {data}")
    if self.process is None:
      return

    try:
      if self.process.stdin.writable(): # if stdin was closed, raise exception
        self.process.stdin.write(data.encode())
        self.process.stdin.write(b"\n")
        self.process.stdin.flush()
        # print(f"STDIN >> {data}")
    except Exception as e:
      print(f"[JavaBuilder] stdin write exception: {e.message}")

  def run_anyway_with_panel(self, is_clean=False):
    if self.process is None:
      raise Exception("The process is not ready")

    proc_start_time = time.time()

    while self.process.poll() is None:
      if self.process.stdout.readable():
        line = self.process.stdout.read()
        if line and len(line) > 0:
          self.__append_output_panel__(line.decode())

    self.process.wait()
    proc_end_time = time.time()

    running_time = proc_end_time - proc_start_time
    returncode = self.process.returncode
    self.__append_output_panel__(f"\n[Running Time {running_time:.6f}s with Exit Code {returncode}]")

    self.process = None

    if is_clean:
      output = self.project.clean()
      self.__append_output_panel__("\n" + output)

  def __append_output_panel__(self, text):
    self.output_panel.run_command("append", {"characters": text, "scroll_to_end": True})

  def __set_build_messages__(self, build_output):
    region_key = "build_messages"

    pattern = r"^(...*?):([0-9]*):?([0-9]*)(.+)"
    matches = re.findall(pattern, build_output, flags=re.M)

    sheets = {}
    for sheet in self.window.sheets():
      key = sheet.file_name()
      try:
        key = Path(key).resolve()
      except:
        pass

      sheets[key] = sheet
      sheet.view().erase_regions(region_key)

    errors = {}
    for match in matches:
      file_path, rows, cols, err_message = match

      file_path = Path(file_path).resolve()
      rows = int(1 if rows == "" else rows)
      cols = int(1 if cols == "" else cols)
      err_message = err_message.strip()

      rows = rows - 1

      if file_path not in errors:
        errors[file_path] = []

      errors[file_path].append({
        "rows": rows,
        "cols": cols,
        "error": err_message,
      })

    for file_path in errors:
      if file_path not in sheets:
        continue

      view = sheets[file_path].view()
      # view.erase_regions(region_key)

      error = errors[file_path]
      regions = []
      messages = []

      for item in error:
        regions.append(sublime.Region(
          view.text_point(item["rows"], item["cols"]),
          view.text_point(item["rows"] + 1, 0) - 1),
        )
        messages.append(item["error"])

      view.add_regions(
        region_key,
        regions,
        "",
        flags=sublime.RegionFlags.NONE,
        annotations=messages,
        annotation_color="region.redish",
        on_close=lambda: view.erase_regions(region_key),
      )


class JavaProject:
  # 프로그램 실행 위치
  original_working_dir = None

  # main 함수를 포함하는 Java 소스 파일 위치
  main_src_path = None

  # Java 패키지명
  package_name = None

  # main 함수를 포함하는 클래스명(패키지명 포함)
  main_class_with_package = None

  # 패키지 최상단 디렉토리
  project_path = None

  # 프로젝트 전체 Java 파일 위치들
  src_files = None

  # 빌드 실행 여부
  is_builded = False

  # 자바 명령어 실행기
  java_runner = None


  def __init__(self):
    self.original_working_dir = os.getcwd()

  def __del__(self):
    os.chdir(self.original_working_dir)

  def clean(self):
    class_files = self.__get_all_java_class_files__(self.project_path)
    for file in class_files:
      file.unlink()
    return f"Cleaning Java build files ({len(class_files)}'s files removed)"

  def run(self):
    input_file = next(Path(self.project_path).glob(f"{self.main_src_path.stem}.in"), None)
    process = self.java_runner.java([self.main_class_with_package])

    if input_file is not None:
      print(f"[JavaBuilder] Found an input file ({input_file.name})")
      with open(input_file, "r") as f:
        while True:
          line = f.readline()
          # print(f"WRITE >>>> {line}")
          if line == '':
            break
          process.stdin.write(line.encode())
      process.stdin.write(b"\n")
      process.stdin.flush()

    return process

  def run_and_output(self):
    process = self.run()
    process.wait()

    if process.returncode == 0:
      return process.stdout.read().decode()
    else:
      return process.stderr.read().decode()

  def build_check(self):
    output = ""
    try:
      output += self.build()
    except:
      self.clean()
    return output

  def build(self):
    if (not self.src_files) or len(self.src_files) == 0:
      raise Exception("Is not ready to build")

    proc = self.java_runner.javac(self.src_files)
    proc.wait()

    if proc.returncode != 0:
      # print(proc.stdout.read().decode())
      return proc.stderr.read().decode()
    
    self.is_builded = True
    return ""

  def setup_java_cli(self, java_home):
    self.java_runner = JavaRunner(java_home)

  def load_project(self, main_src_path):
    self.is_builded = False

    self.main_src_path = Path(main_src_path)
    self.package_name = self.__read_package_name__(self.main_src_path)

    src_file_name = self.main_src_path.stem
    self.main_class_with_package = f"{self.package_name}.{src_file_name}"

    main_dir_path = self.main_src_path
    self.project_path = self.__get_project_path__(main_dir_path, self.package_name)
    self.src_files = self.__get_all_java_files__(self.project_path)

    # Move to project path
    os.chdir(self.project_path)

  def __read_package_name__(self, src_file_path):
    with open(src_file_path, "r") as file:
      first_line = file.readline().strip()
      
      if first_line.startswith("package"):
        package_name = first_line[8:-1]
        return package_name

  def __get_project_path__(self, src_dir_path, package_name):
    relative_path = "".join(["../" for i in package_name.split(".")])

    p = Path(src_dir_path)
    if not p.is_dir():
      p = p.parent

    pwd = p.joinpath(relative_path)
    return pwd

  def __get_all_java_files__(self, root_path):
    return list(Path(root_path).glob("**/*.java"))

  def __get_all_java_class_files__(self, root_path):
    return list(Path(root_path).glob("**/*.class"))


class JavaRunner:
  java_home = None
  java_bin = None

  def __init__(self, java_home):
    self.java_home = Path(java_home)
    self.java_bin = self.java_home.joinpath("bin")

  def java(self, args):
    return self.__run__("java", args)

  def javac(self, args):
    return self.__run__("javac", args)

  def __run__(self, program_name, args):
    program = self.java_bin.joinpath(program_name)

    PIPE = subprocess.PIPE
    return subprocess.Popen([program, *args], stdin=PIPE ,stdout=PIPE, stderr=PIPE)
    
    # return subprocess.run([program, *args], capture_output=True)

