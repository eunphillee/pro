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
