import { NextResponse } from 'next/server';

export async function POST(request: Request) {
  const body = await request.json();
  const userText = body.text;

  if (!userText) {
    return NextResponse.json({ error: 'الرجاء كتابة نص' }, { status: 400 });
  }

  // استدعاء Gemini API
  const response = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=${process.env.GEMINI_API_KEY}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        contents: [{
          parts: [{
            text: `أنت مساعد تسوق ذكي. المستخدم يطلب: "${userText}". 
            استخرج كلمات البحث والميزانية (إن وجدت) وأرجع النتيجة بصيغة JSON فقط بهذا الشكل: 
            {"keywords": "اسم المنتج", "budget": 4000}`
          }]
        }]
      })
    }
  );

  const data = await response.json();
  let aiResult = { keywords: userText, budget: null };
  
  try {
    // محاولة قراءة رد الـ AI
    const textResponse = data.candidates[0].content.parts[0].text;
    const jsonMatch = textResponse.match(/\{.*\}/s);
    if (jsonMatch) aiResult = JSON.parse(jsonMatch[0]);
  } catch (e) {
    console.error("AI parsing error", e);
  }

  return NextResponse.json(aiResult);
}
