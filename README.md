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

## 확장 기능

상단 메뉴의 **확장 기능**에서 사용할 수 있습니다. 기능별 코드는 `features/`에 분리되어 있습니다.

| 파일 | 기능 |
| --- | --- |
| `case_manager.py` | 로컬 케이스·환자·의뢰처 DB, 케이스 ZIP 가져오기·내보내기 |
| `scan_tools.py` | 복사·미러, 강체 정합, 기준점 주변 마진 자동 추적 |
| `tooth_library.py`, `materials.py` | 사용자 치아형·재료 라이브러리 |
| `restorations.py` | 오버레이·베니어·에그쉘·컷백·텔레스코픽·왁스업 등록 |
| `attachments.py` | 일반 어태치먼트·포스트와 코어·스크루 채널, 합집합·차집합 |
| `contacts.py`, `blockout.py` | 인접면·대합면·폰틱 적합, 타원 연결부, 투영 블록아웃 |
| `bars.py` | 경로를 따른 원형·타원형·사각형·반원형 바, 유지공 |
| `model_tools.py` | 다이의 키형 핀·소켓, 한글 모델 표기 |
| `dentures.py`, `splints.py` | 의치상·인공치 소켓·경로 프레임·클라스프, 장치 윤곽·교합면·천공 |
| `articulator.py` | 강체 턱 운동·CSV 가져오기, 동적 접촉 검사·교합 보정 |
| `dicom_tools.py` | DICOM 세 방향 단면·윈도 설정·범위 분할·볼륨 보기 |
| `smile_design.py`, `appearance.py` | 사진과 치아의 2점 정렬·2D 합성, PBR 재질·PNG 저장 |
| `output_tools.py` | 전체 출력 포함 여부, 선택 출력·검사 결과 CSV |

형상 편집 결과는 별도 오브젝트로 생성됩니다. 대체된 원본은 보존하며 전체 출력에서 제외합니다. 출력 포함 여부는 확장 기능에서 변경할 수 있습니다.

턱 운동 CSV의 필수 열은 `tx,ty,tz,rx,ry,rz`이며, 이동 단위는 mm, 회전 단위는 도입니다. 선택 열 `time`은 증가하는 초 단위 시간입니다. 회전 중심은 화면에서 별도로 지정합니다.

케이스 DB와 사용자 재료는 `%LOCALAPPDATA%\EXOCADE`에 저장됩니다. 프로젝트에는 생성 메시·재질·턱 운동 설정이 저장되며, 원본 DICOM과 스마일 합성 화면은 포함되지 않습니다. 스마일 합성은 PNG로 따로 저장합니다.

확장 설계는 자체 기하 알고리즘을 사용하는 프로토타입입니다. 제조사 접속부 규격·전용 라이브러리·전용 장비 API·클라우드 AI 서비스는 포함하지 않습니다.
