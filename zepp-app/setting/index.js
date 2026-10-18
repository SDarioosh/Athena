/*
 * Settings page shown in the Zepp phone app: where the Side Service should upload.
 * Values live in settingsStorage on the phone (never in the repo).
 */
AppSettingsPage({
  build(props) {
    const get = (k) => props.settingsStorage.getItem(k) || ''
    const field = (key, label, placeholder) =>
      TextInput({
        label,
        placeholder,
        value: get(key),
        onChange: (val) => props.settingsStorage.setItem(key, (val || '').trim()),
      })
    return View({ style: { padding: '12px 20px' } }, [
      Text({ bold: true, paragraph: true }, 'convo-ai server'),
      field('serverUrl', 'Server URL', 'http://100.x.y.z:8000 (Tailscale IP)'),
      field('token', 'Ingest token', 'CONVO_INGEST_TOKEN from server/.env'),
      Text(
        { paragraph: true, style: { fontSize: '12px', color: '#666' } },
        `URL: ${get('serverUrl') || '(not set)'} · token: ${get('token') ? 'set' : '(not set)'}`,
      ),
    ])
  },
})
