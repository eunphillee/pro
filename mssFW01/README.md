# mssFW01

MSS03 Alarm Board 펌웨어 (ESP32-S3-WROOM-1-N16R2)

## 개요

- **타깃:** esp32s3
- **프레임워크:** ESP-IDF v5.4.2
- **언어:** C
- **펌웨어 버전:** 0.1.0

Cursor에서 코드를 작성하고, 빌드/플래시는 PowerShell에서 ESP-IDF 명령으로 진행합니다.

## 사전 요구사항

- ESP-IDF v5.4.2 (`C:\Users\user\esp\esp-idf`)
- Python 3.12
- Git

## 빌드

PowerShell에서:

```powershell
C:\Users\user\esp\esp-idf\export.ps1
cd C:\Users\user\Documents\pro\mssFW01
idf.py set-target esp32s3
idf.py build
```

## 플래시 및 모니터

```powershell
idf.py -p COMx flash monitor
```

`COMx`를 실제 시리얼 포트로 바꿉니다.

## 프로젝트 구조

```
mssFW01/
├── CMakeLists.txt
├── README.md
├── PROJECT_SPEC.md
└── main/
    ├── CMakeLists.txt
    ├── app_main.c        # 부팅 시퀀스
    ├── board_pins.h      # GPIO 핀맵
    ├── buzzer.c/h        # 부저 제어
    ├── input.c/h         # 릴레이 입력 모니터
    ├── eeprom.c/h        # I2C EEPROM 스캔
    └── lte_modem.c/h     # LTE UART / PowerKey / Reset
```

## 1차 Bring-up 기능

1. 부팅 로그 출력
2. 부저 100ms ON/OFF 테스트
3. GPIO4/5/6 입력 1초 간격 로그
4. I2C EEPROM (AT24C02C @ 0x50) 스캔
5. LTE UART2 초기화 (115200 8N1)
6. 모뎀 PowerKey 후 3초 대기, AT 명령 테스트
7. LTE PowerKey / Reset 펄스 함수 제공

상세 사양은 [PROJECT_SPEC.md](PROJECT_SPEC.md)를 참고하세요.

=================================================================================================================

1. PowerShell에서 ESP-IDF 환경 잡기

PowerShell을 열고 먼저 이 명령을 입력하세요.

C:\Users\user\esp\esp-idf\export.ps1

이건 PowerShell을 새로 열 때마다 한 번씩 해줘야 합니다.

2. 펌웨어 폴더로 이동
cd C:\Users\user\Documents\pro\mssFW01

현재 위치 확인:

pwd

이렇게 나오면 맞습니다.

C:\Users\user\Documents\pro\mssFW01
3. COM7로 다운로드 + 모니터 실행

이 명령을 입력하세요.

idf.py -p COM7 flash monitor

이게 핵심 명령입니다.

-p COM7        → COM7 포트 사용
flash          → ESP32-S3에 프로그램 다운로드
monitor        → 다운로드 후 로그 보기
4. 정상 진행 화면

정상이면 이런 식으로 진행됩니다.

Connecting...
Chip is ESP32-S3
Writing at 0x00010000...
Hash of data verified.
Hard resetting via RTS pin...

그 다음 모니터 로그가 나옵니다.

MSS03 Alarm Board Boot OK
FW Version: 0.1.0
Target: ESP32-S3-WROOM-1-N16R2

이 로그가 나오면 다운로드 성공 + ESP32 실행 성공입니다.

5. 만약 다운로드가 안 되면

자동 다운로드 회로가 잘 동작하면 그냥 됩니다.
그런데 만약 이런 에러가 나오면:

Failed to connect
Timed out waiting for packet header
Wrong boot mode

그때는 수동 다운로드 모드로 들어가면 됩니다.

수동 다운로드 방법

보드에 버튼이 2개 있을 겁니다.

KEY1 = GPIO0 / BOOT
KEY2 = CHIP_PU / RESET

순서:

1. BOOT 버튼, 즉 GPIO0 버튼을 누른 상태로 유지
2. RESET 버튼을 짧게 한 번 누름
3. RESET 버튼은 떼고, BOOT 버튼은 아직 누르고 있음
4. PowerShell에서 다시 실행:
   idf.py -p COM7 flash monitor
5. Connecting... 이 뜨면 BOOT 버튼을 뗌

간단히 말하면:

BOOT 누르고 있음 → RESET 한번 누름 → flash 실행 → 연결되면 BOOT 뗌
6. 모니터 종료 방법

monitor 화면에서 빠져나오려면 키보드에서:

Ctrl + ]

를 누르시면 됩니다.

7. 지금 바로 입력할 명령 전체

그냥 아래 순서대로 하시면 됩니다.

C:\Users\user\esp\esp-idf\export.ps1
cd C:\Users\user\Documents\pro\mssFW01
idf.py -p COM7 flash monitor




