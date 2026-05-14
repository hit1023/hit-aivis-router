const FETCH_OPT = { muteHttpExceptions: true };

// --- 接続先管理 ---

function getEndpoints() {
  const raw = PropertiesService.getScriptProperties().getProperty('API_ENDPOINTS');
  return raw ? JSON.parse(raw) : [];
}

function saveEndpoints(endpoints) {
  PropertiesService.getScriptProperties().setProperty('API_ENDPOINTS', JSON.stringify(endpoints));
}

function getApiUrl() {
  const props = PropertiesService.getScriptProperties();
  const currentName = props.getProperty('API_CURRENT');
  const endpoints = getEndpoints();
  if (!endpoints.length) return '';
  const found = endpoints.find(e => e.name === currentName);
  return (found || endpoints[0]).url;
}

function getCurrentName() {
  const props = PropertiesService.getScriptProperties();
  const currentName = props.getProperty('API_CURRENT');
  const endpoints = getEndpoints();
  if (!endpoints.length) return '未設定';
  const found = endpoints.find(e => e.name === currentName);
  return (found || endpoints[0]).name;
}

function switchToEndpoint(name) {
  PropertiesService.getScriptProperties().setProperty('API_CURRENT', name);
}

function addEndpoint(name, url) {
  const endpoints = getEndpoints();
  const existing = endpoints.findIndex(e => e.name === name);
  if (existing >= 0) {
    endpoints[existing].url = url;
  } else {
    endpoints.push({ name, url });
  }
  saveEndpoints(endpoints);
  if (endpoints.length === 1) {
    PropertiesService.getScriptProperties().setProperty('API_CURRENT', name);
  }
}

function deleteEndpoint(name) {
  const endpoints = getEndpoints().filter(e => e.name !== name);
  saveEndpoints(endpoints);
  const props = PropertiesService.getScriptProperties();
  if (props.getProperty('API_CURRENT') === name) {
    props.setProperty('API_CURRENT', endpoints.length ? endpoints[0].name : '');
  }
}

// --- メニュー ---

function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu('AivisSpeech')
    .addItem('単語辞書をAPIから取得', 'syncUserDictFromApi')
    .addItem('単語辞書をAPIへ送る', 'importUserDict')
    .addSeparator()
    .addItem('置換ルールをAPIから取得', 'syncReplacementsFromApi')
    .addItem('置換ルールをAPIへ送る', 'importTextReplacements')
    .addSeparator()
    .addSubMenu(ui.createMenu('🔗 接続先')
      .addItem('切り替え', 'showSwitchDialog')
      .addItem('追加・編集', 'showAddDialog')
      .addItem('削除', 'showDeleteDialog')
      .addSeparator()
      .addItem('現在の接続先を確認', 'showApiUrl')
    )
    .addToUi();
}

function showApiUrl() {
  const endpoints = getEndpoints();
  if (!endpoints.length) {
    SpreadsheetApp.getUi().alert('接続先が登録されていません。\n「接続先 → 追加・編集」から登録してください。');
    return;
  }
  SpreadsheetApp.getUi().alert(`現在の接続先\n名前: ${getCurrentName()}\nURL: ${getApiUrl()}`);
}

// --- ダイアログ ---

function showSwitchDialog() {
  const endpoints = getEndpoints();
  if (!endpoints.length) {
    SpreadsheetApp.getUi().alert('接続先が登録されていません。\n「接続先 → 追加・編集」から登録してください。');
    return;
  }
  const current = getCurrentName();
  const options = endpoints.map(e =>
    `<option value="${escHtml(e.name)}" ${e.name === current ? 'selected' : ''}>${escHtml(e.name)} — ${escHtml(e.url)}</option>`
  ).join('');

  const html = HtmlService.createHtmlOutput(`
    <style>
      body { font-family: sans-serif; padding: 16px; }
      select { width: 100%; padding: 6px; font-size: 14px; margin-top: 8px; }
      .buttons { margin-top: 16px; text-align: right; }
      button { padding: 6px 16px; margin-left: 8px; font-size: 13px; cursor: pointer; }
      .ok { background: #1a73e8; color: white; border: none; border-radius: 4px; }
      .cancel { background: white; border: 1px solid #ccc; border-radius: 4px; }
    </style>
    <body>
      <div>接続先を選択してください:</div>
      <select id="sel">${options}</select>
      <div class="buttons">
        <button class="cancel" onclick="google.script.host.close()">キャンセル</button>
        <button class="ok" onclick="doSwitch()">切り替え</button>
      </div>
      <script>
        function doSwitch() {
          const name = document.getElementById('sel').value;
          google.script.run.withSuccessHandler(() => {
            google.script.host.close();
          }).switchToEndpoint(name);
        }
      </script>
    </body>
  `).setWidth(480).setHeight(150);
  SpreadsheetApp.getUi().showModalDialog(html, '接続先を切り替え');
}

function showAddDialog() {
  const ui = SpreadsheetApp.getUi();
  const nameRes = ui.prompt('接続先を追加・編集', '名前を入力してください（例: 本番、検証）:', ui.ButtonSet.OK_CANCEL);
  if (nameRes.getSelectedButton() !== ui.Button.OK) return;
  const name = nameRes.getResponseText().trim();
  if (!name) return;

  const endpoints = getEndpoints();
  const existing = endpoints.find(e => e.name === name);
  const urlRes = ui.prompt('接続先を追加・編集', `URL を入力してください:\n${existing ? `現在: ${existing.url}` : ''}`, ui.ButtonSet.OK_CANCEL);
  if (urlRes.getSelectedButton() !== ui.Button.OK) return;
  const url = urlRes.getResponseText().trim().replace(/\/$/, '');
  if (!url) return;

  addEndpoint(name, url);
  ui.alert(`登録しました\n名前: ${name}\nURL: ${url}`);
}

function showDeleteDialog() {
  const endpoints = getEndpoints();
  if (!endpoints.length) {
    SpreadsheetApp.getUi().alert('登録された接続先がありません。');
    return;
  }
  const current = getCurrentName();
  const options = endpoints.map(e =>
    `<option value="${escHtml(e.name)}" ${e.name === current ? 'selected' : ''}>${escHtml(e.name)} — ${escHtml(e.url)}</option>`
  ).join('');

  const html = HtmlService.createHtmlOutput(`
    <style>
      body { font-family: sans-serif; padding: 16px; }
      select { width: 100%; padding: 6px; font-size: 14px; margin-top: 8px; }
      .buttons { margin-top: 16px; text-align: right; }
      button { padding: 6px 16px; margin-left: 8px; font-size: 13px; cursor: pointer; }
      .del { background: #d93025; color: white; border: none; border-radius: 4px; }
      .cancel { background: white; border: 1px solid #ccc; border-radius: 4px; }
    </style>
    <body>
      <div>削除する接続先を選択してください:</div>
      <select id="sel">${options}</select>
      <div class="buttons">
        <button class="cancel" onclick="google.script.host.close()">キャンセル</button>
        <button class="del" onclick="doDel()">削除</button>
      </div>
      <script>
        function doDel() {
          const name = document.getElementById('sel').value;
          google.script.run.withSuccessHandler(() => {
            google.script.host.close();
          }).deleteEndpoint(name);
        }
      </script>
    </body>
  `).setWidth(480).setHeight(150);
  SpreadsheetApp.getUi().showModalDialog(html, '接続先を削除');
}

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// --- 単語辞書・置換ルール ---

function syncUserDictFromApi() {
  const API_URL = getApiUrl();
  if (!API_URL) { SpreadsheetApp.getUi().alert('接続先が未設定です。「接続先 → 追加・編集」から登録してください。'); return; }
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName('単語辞書');
  let mergeMode = false;
  if (sheet && sheet.getLastRow() > 1) {
    const ui = SpreadsheetApp.getUi();
    const res = ui.alert(
      '単語辞書を更新します',
      `シートに既存のデータ（${sheet.getLastRow() - 1} 件）があります。\n\n` +
      '「はい」: APIの内容で全件上書き\n' +
      '「いいえ」: 既存データを保持しながら差分更新（新規追加・変更のみ反映）\n' +
      '「キャンセル」: 中止',
      ui.ButtonSet.YES_NO_CANCEL
    );
    if (res === ui.Button.CANCEL) return;
    mergeMode = (res === ui.Button.NO);
  }
  if (!sheet) sheet = ss.insertSheet('単語辞書');

  const dictRes   = UrlFetchApp.fetch(`${API_URL}/user_dict?enable_compound_accent=true`, FETCH_OPT);
  const splitsRes = UrlFetchApp.fetch(`${API_URL}/user_dict/compound_splits`, FETCH_OPT);
  if (dictRes.getResponseCode() !== 200) { SpreadsheetApp.getUi().alert('取得失敗: ' + dictRes.getContentText()); return; }

  const dict      = JSON.parse(dictRes.getContentText());
  const splitsRaw = splitsRes.getResponseCode() === 200 ? JSON.parse(splitsRes.getContentText()) : {};

  function getSplitInfo(raw) {
    if (!raw) return null;
    return Array.isArray(raw) ? { surface: raw } : raw;
  }

  const rows = [['表層形', '読み', 'アクセント', '品詞', '優先度']];
  for (const [uuid, entry] of Object.entries(dict)) {
    if (typeof entry !== 'object') continue;
    const surfaceKey = typeof entry.surface === 'string' ? entry.surface : (entry.surface || []).join('');
    const splitInfo  = getSplitInfo(splitsRaw[surfaceKey]);
    const splitList  = splitInfo ? splitInfo.surface : null;

    let pronArr = Array.isArray(entry.pronunciation) ? entry.pronunciation : [entry.pronunciation || ''];
    let atArr   = Array.isArray(entry.accent_type)   ? entry.accent_type   : [entry.accent_type ?? 0];

    if (splitInfo && splitInfo.pronunciation && pronArr.length === 1 && splitInfo.pronunciation.length > 1) {
      pronArr = splitInfo.pronunciation;
      atArr   = splitInfo.accent_type || atArr;
    }

    const surfaceDisplay = splitList ? splitList.join('|') : surfaceKey;
    const wordType       = resolveWordTypeLabel(entry.word_type);
    rows.push([surfaceDisplay, pronArr.join('|'), atArr.join('|'), wordType, entry.priority ?? 5]);
  }

  if (mergeMode) {
    const existingData = sheet.getDataRange().getValues();
    const existingMap = {};
    for (let i = 1; i < existingData.length; i++) {
      if (existingData[i][0]) existingMap[String(existingData[i][0])] = i + 1;
    }
    let updated = 0, added = 0;
    for (const row of rows.slice(1)) {
      const surface = String(row[0]);
      if (existingMap[surface] !== undefined) {
        sheet.getRange(existingMap[surface], 1, 1, 5).setValues([row]);
        updated++;
      } else {
        sheet.appendRow(row);
        added++;
      }
    }
    setWordTypeValidation(sheet);
    SpreadsheetApp.getUi().alert(`差分更新完了: 更新 ${updated} 件 / 追加 ${added} 件\n接続先: ${getCurrentName()}`);
  } else {
    sheet.clearContents();
    sheet.getRange(1, 1, rows.length, 5).setValues(rows);
    setWordTypeValidation(sheet);
    SpreadsheetApp.getUi().alert(`取得完了（全件上書き）: ${rows.length - 1} 件\n接続先: ${getCurrentName()}`);
  }
}

function importUserDict() {
  const API_URL = getApiUrl();
  if (!API_URL) { SpreadsheetApp.getUi().alert('接続先が未設定です。'); return; }
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('単語辞書');
  if (!sheet) { SpreadsheetApp.getUi().alert('「単語辞書」シートが見つかりません'); return; }

  const data = sheet.getDataRange().getValues();
  let inserted = 0, errors = [];

  for (let i = 1; i < data.length; i++) {
    const [surfaceRaw, pronRaw, accentRaw, wordType, priority] = data[i];
    if (!surfaceRaw) continue;

    const surfaceList = String(surfaceRaw).split('|').map(s => s.trim()).filter(Boolean);
    const pronList    = String(pronRaw).split('|').map(p => p.trim()).filter(Boolean);
    const accentList  = String(accentRaw).split('|').map(a => parseInt(a) || 0);

    const payload = {
      surface:       surfaceList,
      pronunciation: pronList.length ? pronList : surfaceList,
      accent_type:   accentList.length === surfaceList.length ? accentList : surfaceList.map(() => 0),
      word_type:     resolveWordType(String(wordType)),
      priority:      parseInt(priority) || 5,
    };

    const res = UrlFetchApp.fetch(`${API_URL}/user_dict`, {
      method: 'post', contentType: 'application/json',
      payload: JSON.stringify(payload), muteHttpExceptions: true,
    });

    if (res.getResponseCode() === 200) inserted++;
    else errors.push(`行${i + 1}: ${res.getContentText()}`);
  }

  const msg = `送信完了: ${inserted} 件\n接続先: ${getCurrentName()}` + (errors.length ? `\nエラー:\n${errors.slice(0, 5).join('\n')}` : '');
  SpreadsheetApp.getUi().alert(msg);
}

function syncReplacementsFromApi() {
  const API_URL = getApiUrl();
  if (!API_URL) { SpreadsheetApp.getUi().alert('接続先が未設定です。'); return; }
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName('置換ルール');
  let mergeMode = false;
  if (sheet && sheet.getLastRow() > 1) {
    const ui = SpreadsheetApp.getUi();
    const res = ui.alert(
      '置換ルールを更新します',
      `シートに既存のデータ（${sheet.getLastRow() - 1} 件）があります。\n\n` +
      '「はい」: APIの内容で全件上書き\n' +
      '「いいえ」: 既存データを保持しながら差分更新（新規追加・変更のみ反映）\n' +
      '「キャンセル」: 中止',
      ui.ButtonSet.YES_NO_CANCEL
    );
    if (res === ui.Button.CANCEL) return;
    mergeMode = (res === ui.Button.NO);
  }
  if (!sheet) sheet = ss.insertSheet('置換ルール');

  const res = UrlFetchApp.fetch(`${API_URL}/text_replacements`, FETCH_OPT);
  if (res.getResponseCode() !== 200) { SpreadsheetApp.getUi().alert('取得失敗'); return; }

  const rules = JSON.parse(res.getContentText());
  const rows  = [['置換前', '置換後'], ...Object.entries(rules)];

  if (mergeMode) {
    const existingData = sheet.getDataRange().getValues();
    const existingMap = {};
    for (let i = 1; i < existingData.length; i++) {
      if (existingData[i][0]) existingMap[String(existingData[i][0])] = i + 1;
    }
    let updated = 0, added = 0;
    for (const row of rows.slice(1)) {
      const src = String(row[0]);
      if (existingMap[src] !== undefined) {
        sheet.getRange(existingMap[src], 1, 1, 2).setValues([row]);
        updated++;
      } else {
        sheet.appendRow(row);
        added++;
      }
    }
    SpreadsheetApp.getUi().alert(`差分更新完了: 更新 ${updated} 件 / 追加 ${added} 件\n接続先: ${getCurrentName()}`);
  } else {
    sheet.clearContents();
    sheet.getRange(1, 1, rows.length, 2).setValues(rows);
    SpreadsheetApp.getUi().alert(`取得完了（全件上書き）: ${rows.length - 1} 件\n接続先: ${getCurrentName()}`);
  }
}

function importTextReplacements() {
  const API_URL = getApiUrl();
  if (!API_URL) { SpreadsheetApp.getUi().alert('接続先が未設定です。'); return; }
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('置換ルール');
  if (!sheet) { SpreadsheetApp.getUi().alert('「置換ルール」シートが見つかりません'); return; }

  const data  = sheet.getDataRange().getValues().slice(1);
  const rules = {};
  for (const [src, dst] of data) {
    if (src) rules[String(src)] = String(dst);
  }

  const boundary = '----boundary';
  const csv      = Object.entries(rules).map(([k, v]) => `${k},${v}`).join('\n');
  const body     = `--${boundary}\r\nContent-Disposition: form-data; name="mode"\r\n\r\nupsert\r\n--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="rules.csv"\r\nContent-Type: text/csv\r\n\r\n${csv}\r\n--${boundary}--`;

  const res = UrlFetchApp.fetch(`${API_URL}/text_replacements/import`, {
    method: 'post',
    contentType: `multipart/form-data; boundary=${boundary}`,
    payload: body, muteHttpExceptions: true,
  });

  const result = JSON.parse(res.getContentText());
  SpreadsheetApp.getUi().alert(`送信完了: 追加 ${result.inserted} 件, 更新 ${result.updated} 件\n接続先: ${getCurrentName()}`);
}

// --- 品詞変換 ---

function resolveWordType(label) {
  const map = {
    '固有名詞':      'PROPER_NOUN',
    '地名':          'LOCATION_NAME',
    '組織・施設名':  'ORGANIZATION_NAME',
    '人名':          'PERSON_NAME',
    '人名（姓）':    'PERSON_FAMILY_NAME',
    '人名（名）':    'PERSON_GIVEN_NAME',
    '普通名詞':      'COMMON_NOUN',
    '動詞':          'VERB',
    '形容詞':        'ADJECTIVE',
    '語尾':          'SUFFIX',
  };
  return map[label] || 'PROPER_NOUN';
}

function setWordTypeValidation(sheet) {
  const wordTypes = [
    '固有名詞', '地名', '組織・施設名', '人名', '人名（姓）', '人名（名）',
    '普通名詞', '動詞', '形容詞', '語尾',
  ];
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(wordTypes, true)
    .setAllowInvalid(true)
    .build();
  const lastRow = Math.max(sheet.getLastRow(), 2);
  sheet.getRange(2, 4, lastRow - 1, 1).setDataValidation(rule);
}

function resolveWordTypeLabel(wordType) {
  const map = {
    'PROPER_NOUN':        '固有名詞',
    'LOCATION_NAME':      '地名',
    'ORGANIZATION_NAME':  '組織・施設名',
    'PERSON_NAME':        '人名',
    'PERSON_FAMILY_NAME': '人名（姓）',
    'PERSON_GIVEN_NAME':  '人名（名）',
    'COMMON_NOUN':        '普通名詞',
    'VERB':               '動詞',
    'ADJECTIVE':          '形容詞',
    'SUFFIX':             '語尾',
  };
  return map[wordType] || '固有名詞';
}