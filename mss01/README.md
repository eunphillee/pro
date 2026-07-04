# Cat.M1 LTE 모뎀 SMS 발송 테스트 프로그램

Windows 11에서 USB로 연결된 우리넷 LTE Cat.M1 모뎀의 COM 포트를 열고, 기본 AT 명령과 SMS 발송을 테스트하는 PySide6 GUI 프로그램입니다.

## 기능

- COM 포트 선택 및 새로고침
- Baudrate 선택, 기본값 `115200`
- 연결/해제
- 기본 AT 명령 버튼: `AT`, `ATI`, `AT+CSQ`, `AT+CREG?`, `AT+COPS?`
- 직접 AT 명령 입력 및 전송
- 전화번호/문자내용 입력 후 SMS 전송
- 모든 송신/수신 로그 화면 표시
- 초보자용 단순 UI

## 설치

PowerShell에서 프로젝트 폴더로 이동한 뒤 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 실행

```powershell
python main.py
```

Python이 PATH에 없으면 아래처럼 실행해도 됩니다.

```powershell
py main.py
```

Codex에서 제가 사용한 번들 Python으로 실행하려면:

```powershell
& "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" main.py
```

## 가장 쉬운 실행 방법

1. PowerShell을 엽니다.
2. 프로젝트 폴더로 이동합니다.

```powershell
cd C:\Users\user\Documents\pro\mss01
```

3. 가상환경을 만들었다면 활성화합니다.

```powershell
.\.venv\Scripts\Activate.ps1
```

4. 프로그램을 실행합니다.

```powershell
python main.py
```

또는

```powershell
py main.py
```

## 사용 순서

1. 모뎀을 USB로 PC에 연결합니다.
2. Windows 장치관리자에서 모뎀이 `COMx` 포트로 잡혔는지 확인합니다.
3. 프로그램을 실행하고 COM 포트와 Baudrate를 선택합니다.
4. `연결` 버튼을 누릅니다.
5. `AT` 버튼을 눌러 `OK` 응답이 오는지 확인합니다.
6. `ATI`, `AT+CSQ`, `AT+CREG?`, `AT+COPS?`로 모뎀/신호/망 등록 상태를 확인합니다.
7. 전화번호와 문자 내용을 입력하고 `SMS 보내기`를 누릅니다.

## SMS 전송 방식

프로그램은 기본 AT Command 텍스트 모드로 SMS를 전송합니다.

```text
ATE0
AT+CMGF=1
AT+CSCS="GSM"
AT+CMGS="전화번호"
문자내용 + Ctrl+Z
```

영문/숫자 메시지 테스트를 먼저 권장합니다. 한글 SMS는 모뎀 펌웨어와 통신사 설정에 따라 UCS2/PDU 모드 설정이 필요할 수 있습니다.

## 문제 해결

- COM 포트가 보이지 않으면 `새로고침`을 누르거나 장치관리자에서 드라이버 설치 상태를 확인하세요.
- `AT`에 응답이 없으면 Baudrate를 `115200`, `9600` 등으로 바꿔 테스트하세요.
- `AT+CREG?` 응답이 등록 상태가 아니면 망 등록 후 SMS를 다시 시도하세요.
- `AT+CSQ` 값이 낮으면 안테나 연결과 수신 환경을 확인하세요.
