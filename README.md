# pro — MSS03 무인경보시스템

ESP32-S3 기반 **MSS03 Alarm Board** 펌웨어와, LTE Cat.M1 모뎀 SMS 검증용 PC 테스트 도구를 포함하는 저장소입니다.

## 저장소 구성

| 폴더 | 설명 | 언어 |
|------|------|------|
| [mssFW01/](mssFW01/) | ESP32-S3 보드 펌웨어 (ESP-IDF v5.4.2) | C |
| [mss01/](mss01/) | Windows PC용 LTE SMS/GPS 테스트 GUI | Python |

## 하드웨어

- **MCU:** ESP32-S3-WROOM-1-N16R2
- **보드:** MSS03 Alarm Board
- **LTE:** Cat.M1 모뎀 (UART2, 115200 bps)
- **입력:** GPIO4/5/6 (LOW Active)
- **부저:** GPIO8
- **EEPROM:** AT24C02C @ I2C 0x50

## mssFW01 — 펌웨어

무인경보시스템용 ESP-IDF 펌웨어입니다.

### 주요 기능

- 부팅 bring-up (부저, EEPROM, LTE AT 테스트)
- 입력 3채널 LOW Active 감시 (1초 간격)
- 입력 조합별 알람 메시지 선택
- 이상 입력 발생 시 LTE SMS 1회 발송 (`01026844484`)
- 동일 입력 mask 반복 SMS 방지 (`last_sent_mask`)

### 빌드

```powershell
C:\Users\user\esp\esp-idf\export.ps1
cd mssFW01
idf.py set-target esp32s3
idf.py build
```

### 플래시 및 모니터

```powershell
idf.py -p COM7 flash monitor
```

상세 사양: [mssFW01/PROJECT_SPEC.md](mssFW01/PROJECT_SPEC.md)

## mss01 — PC SMS 테스트

모뎀을 USB로 PC에 연결해 AT 명령 및 SMS 발송을 검증하는 PySide6 프로그램입니다.  
**mssFW01 LTE SMS 로직은 이 프로젝트의 성공 사례를 참고해 구현되었습니다.**

### 실행

```powershell
cd mss01
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## 개발 환경

| 항목 | 버전 |
|------|------|
| OS | Windows 11 |
| ESP-IDF | v5.4.2 |
| Python | 3.12 |
| IDE | Cursor (코드 작성) |
| 빌드/플래시 | PowerShell + `idf.py` |

## 입력 → SMS 메시지 매핑

| 입력 조합 | 메시지 |
|-----------|--------|
| IN1만 ON | 침수위험(신천IC 배수펌프 #4,5) |
| IN2만 ON | 대피하세요(신천IC 배수펌프 #4,5) |
| IN3만 ON | 가스 이상 감지(O2) |
| IN1+IN2 | 가스 이상 감지(CO) |
| IN2+IN3 | 가스 이상 감지(H2S) |
| IN1+IN3 | 가스 이상 감지(LEL) |
| IN1+IN2+IN3 | 가스 이상 감지(CO2) |
| 모두 OFF | SMS 발송 없음 |

입력 해석: **GPIO raw=0 → ON**, **raw=1 → OFF** (LOW Active)

## 라이선스

내부 프로젝트 — 구로물산 MSS03 무인경보시스템
