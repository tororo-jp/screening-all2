export async function onRequestGet(context) {
  const url = new URL(context.request.url);
  const code = url.searchParams.get('code');

  if (!code || !/^\d{4}$/.test(code)) {
    return json({error: 'Invalid code'}, 400);
  }

  const yfUrl =
    `https://query2.finance.yahoo.com/v8/finance/chart/${code}.T` +
    `?interval=1wk&range=1y&includePrePost=false`;

  try {
    const resp = await fetch(yfUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible)',
        'Accept': 'application/json',
      },
    });
    if (!resp.ok) throw new Error(`Yahoo Finance HTTP ${resp.status}`);

    const data = await resp.json();
    const result = data?.chart?.result?.[0];
    if (!result) throw new Error('No chart data returned');

    const timestamps = result.timestamp;
    const closes    = result.indicators.quote[0].close;

    const series = timestamps
      .map((t, i) => ({time: t, value: closes[i]}))
      .filter(d => d.value != null);

    return json({series}, 200, {'Cache-Control': 'public, max-age=3600'});
  } catch (e) {
    return json({error: e.message}, 500);
  }
}

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      ...extra,
    },
  });
}
