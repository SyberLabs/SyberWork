// The only part of next/server the Flyspace routes under test use: NextResponse.json.
export const NextResponse = {
  json(body, init = {}) {
    return { status: init.status ?? 200, body };
  }
};
export class NextRequest extends Request {}
