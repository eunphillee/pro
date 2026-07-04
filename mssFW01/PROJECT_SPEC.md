# PROJECT_SPEC — mssFW01

## 보드

| 항목 | 값 |
|------|-----|
| 보드 | ESP32-S3-WROOM-1-N16R2 |
| 타깃 | esp32s3 |
| ESP-IDF | v5.4.2 |
| FW 버전 | 0.1.0 |

## 핀맵 (`main/board_pins.h`)

| 신호 | GPIO |
|------|------|
| LTE_RX | 17 |
| LTE_TX | 18 |
| LTE_RESET | 13 |
| LTE_POWERKEY | 12 |
| BUZZER_CTRL | 8 |
| RELAY_IN1 | 4 |
| RELAY_IN2 | 5 |
| RELAY_IN3 | 6 |
| I2C_SDA | 1 |
| I2C_SCL | 2 |
| RGB_LED | 38 |
| USB_D_MINUS | 19 |
| USB_D_PLUS | 20 |
| BOOT_GPIO | 0 |

## 모듈

### app_main.c

부팅 시퀀스 오케스트레이션. 각 하드웨어 모듈을 초기화하고 1차 bring-up을 수행합니다.

### buzzer

- GPIO8 출력
- `buzzer_test()`: 100ms HIGH 후 LOW

### input

- GPIO4, GPIO5, GPIO6 입력 (내부 풀업)
- FreeRTOS 태스크로 1초마다 레벨 로그 출력

### eeprom

- I2C0, SDA=GPIO1, SCL=GPIO2, 100kHz
- `i2c_master_probe()`로 AT24C02C 주소 0x50 확인

### lte_modem

- UART2, TX=GPIO18, RX=GPIO17, 115200bps, 8N1, flow control 없음
- `lte_modem_powerkey_pulse()`: GPIO12 HIGH 2.2초 후 LOW
  - NPN 트랜지스터로 Power Key를 Low로 당기는 회로 기준
  - **실제 보드에서 극성 확인 필요**
- `lte_modem_reset_pulse()`: GPIO13 HIGH 2.2초 후 LOW
  - NPN 트랜지스터로 RESET을 Low로 당기는 회로 기준
  - **실제 보드에서 극성 확인 필요**
- `lte_modem_send_at_test()`: `AT\r\n` 전송, 3초 timeout, 응답 로그

## 부팅 시퀀스

1. 부팅 배너 로그
2. buzzer / input 초기화
3. 부저 테스트
4. 입력 모니터 태스크 시작
5. I2C EEPROM 스캔
6. LTE UART 초기화
7. LTE PowerKey 펄스 (모뎀 전원)
8. 3초 대기
9. AT 명령 테스트

## 코딩 규칙

- Arduino 사용 금지
- ESP-IDF C API만 사용
- 로그: `ESP_LOGI`, `ESP_LOGW`, `ESP_LOGE`
- 지연: `vTaskDelay(pdMS_TO_TICKS(ms))`
- GPIO 번호는 `board_pins.h`에만 정의
- 모듈별 `.c` / `.h` 분리

## 개발 워크플로

- **코드 작성:** Cursor
- **빌드/플래시:** Windows PowerShell + ESP-IDF CLI
- VS Code ESP-IDF 확장은 사용하지 않음

## 향후 확장 (미구현)

- RGB_LED (GPIO38)
- USB (GPIO19/20)
- EEPROM 읽기/쓰기
- LTE 데이터 통신 및 PPP
