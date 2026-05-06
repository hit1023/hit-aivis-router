#!/bin/bash

BLUE='\033[0;34m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
GRAY='\033[0;90m'
BOLD='\033[1m'
RESET='\033[0m'

show_menu() {
  clear
  echo -e "${CYAN}${BOLD}"
  echo "  ╔══════════════════════════════════╗"
  echo "  ║     AIVIS Router  管理メニュー   ║"
  echo "  ╚══════════════════════════════════╝"
  echo -e "${RESET}"
  echo -e "  ${BOLD}1.${RESET}  ${GREEN}ビルド & 起動${RESET}   ${GRAY}(down → build → up)${RESET}"
  echo -e "  ${BOLD}2.${RESET}  ${GREEN}起動${RESET}            ${GRAY}(up -d)${RESET}"
  echo -e "  ${BOLD}3.${RESET}  ${YELLOW}再起動${RESET}          ${GRAY}(restart)${RESET}"
  echo -e "  ${BOLD}4.${RESET}  ${RED}停止${RESET}            ${GRAY}(down)${RESET}"
  echo -e "  ${BOLD}5.${RESET}  ${BLUE}ログを見る${RESET}      ${GRAY}(logs -f)${RESET}"
  echo -e "  ${BOLD}6.${RESET}  ${BLUE}状態を確認${RESET}      ${GRAY}(ps)${RESET}"
  echo -e "  ${BOLD}0.${RESET}  終了"
  echo ""
  echo -ne "  番号を選択 → "
}

while true; do
  show_menu
  read -r choice

  case $choice in
    1)
      echo -e "\n${GREEN}▶ ビルド & 起動${RESET}"
      docker compose down
      docker compose up -d --build
      echo -e "${GREEN}✓ 完了${RESET}"
      echo -e "${GRAY}Enterで戻る...${RESET}"
      read -r
      ;;
    2)
      echo -e "\n${GREEN}▶ 起動${RESET}"
      docker compose up -d
      echo -e "${GREEN}✓ 完了${RESET}"
      echo -e "${GRAY}Enterで戻る...${RESET}"
      read -r
      ;;
    3)
      echo -e "\n${YELLOW}▶ 再起動${RESET}"
      docker compose restart
      echo -e "${GREEN}✓ 完了${RESET}"
      echo -e "${GRAY}Enterで戻る...${RESET}"
      read -r
      ;;
    4)
      echo -e "\n${RED}▶ 停止${RESET}"
      docker compose down
      echo -e "${GREEN}✓ 完了${RESET}"
      echo -e "${GRAY}Enterで戻る...${RESET}"
      read -r
      ;;
    5)
      echo -e "\n${BLUE}▶ ログ表示 ${GRAY}(Ctrl+C で戻る)${RESET}\n"
      docker compose logs -f
      ;;
    6)
      echo -e "\n${BLUE}▶ 状態確認${RESET}\n"
      docker compose ps
      echo -e "\n${GRAY}Enterで戻る...${RESET}"
      read -r
      ;;
    0)
      echo -e "\n${GRAY}終了します${RESET}\n"
      exit 0
      ;;
    *)
      echo -e "\n${RED}無効な番号です${RESET}"
      sleep 1
      ;;
  esac
done
