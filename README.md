# Smart Attendance System — Full Web Conversion

This project converts the uploaded Tkinter/Python Smart Attendance workflow into a browser architecture: HTML + CSS + JavaScript frontend, Vercel serverless API, and Neon PostgreSQL persistence.

## Included workflow
- Letter Flow welcome screen
- Admin / Teacher / Viewer login roles
- 8 lecture timetable with the same default subjects/times
- Sunday + configured holiday attendance block
- Student registration with parent mobile/email
- Browser camera using `getUserMedia`
- Face registration + face recognition using `face-api.js` descriptors
- Present in first 5 minutes; Late after the grace period
- Temporary OUT with Toilet / Water reason
- Temporary IN after the recognized student returns
- Final OUT in last 5 minutes and automatic lecture-end closing
- One attendance row per Student + Date + Lecture
- Attendance, movement and student reports
- CSV export and browser print/PDF
- Holiday calendar
- Parent/student notification queue
- Performance / low attendance / profile / audit / health / tamper / QR / backup information
- Neon persistence when DATABASE_URL is configured; localStorage fallback for testing

## Important camera note
The desktop program uses OpenCV Haar Cascade + LBPH (`face_model.yml`). A browser cannot directly execute that OpenCV LBPH model. The web build therefore uses browser-compatible face-api.js descriptors. The attendance timing, movement, duplicate protection and final-out rules are implemented in JavaScript to match the desktop workflow.

## Deploy on Vercel
1. Upload this folder to GitHub.
2. Import the repository into Vercel.
3. Add `DATABASE_URL` in Vercel Environment Variables using a Neon PostgreSQL connection string.
4. Deploy.
5. Open the deployed HTTPS URL and allow camera permission.

Default demo accounts are `admin`, `teacher`, and `viewer`; password is `1234`. Change these before real deployment by adding a proper authentication layer.

## Local browser test
The frontend can open with a static server, but camera access is normally allowed only on HTTPS or localhost. Example:

`python -m http.server 8000`

Then open `http://localhost:8000`.

For persistent database/API testing, deploy the API to Vercel with `DATABASE_URL`.
