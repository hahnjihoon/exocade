# EXOCADE

Windows에서 실행하는 치과용 3D 모델링 데스크톱 프로그램입니다.

## 준비 사항

- Windows 64비트
- Python 3.11 64비트: 설치할 때 Python을 PATH에 추가합니다.
- Git

## 프로젝트 받기

PowerShell에서 실행합니다.

```powershell
git clone https://github.com/hahnjihoon/exocade.git
cd exocade
```

이후 명령은 모두 `app.py`, `build.ps1`이 있는 프로젝트 루트에서 실행합니다.

## 소스로 실행

빌드 없이 실행할 수 있습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

최초 설치를 마친 뒤에는 다음 명령으로 실행합니다.

```powershell
.\.venv\Scripts\python.exe app.py
```

## EXE 빌드

EXE 파일이 필요하면 프로젝트 루트에서 실행합니다. 빌드 스크립트가 `requirements.txt`의 의존성을 설치하며, Pillow는 아이콘 생성에 필요합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install pillow
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

완료되면 **프로젝트 루트에 `EXOCADE.exe`가 생성됩니다.**

```powershell
.\EXOCADE.exe
```

## 다른 PC에 전달

빌드한 `EXOCADE.exe`만 전달하면 됩니다. 실행하는 PC에는 프로젝트 소스나 Python 설치가 필요하지 않습니다.

출력 준비에 사용하는 슬라이서는 해당 PC에 별도로 설치하고, 프로그램에서 슬라이서와 프린터를 설정해야 합니다.

EXE, 가상환경(`.venv`), 빌드 임시파일(`build`)은 Git 업로드 대상에서 제외되어 있습니다.
