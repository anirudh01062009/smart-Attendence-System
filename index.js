import { neon } from '@neondatabase/serverless';
import crypto from 'crypto';

const sql = process.env.DATABASE_URL ? neon(process.env.DATABASE_URL) : null;
const DEFAULT_LECTURES=[
 [1,'Data Structures','09:00','10:00'],[2,'Operating Systems','10:00','11:00'],[3,'DBMS','11:15','12:15'],
 [4,'Computer Networks','12:15','13:15'],[5,'Python','14:00','15:00'],[6,'AI & ML','15:00','16:00'],
 [7,'Web Technology','16:00','17:00'],[8,'Project','17:00','18:00']
];
const hash=p=>crypto.createHash('sha256').update(String(p)).digest('hex');
const today=()=>new Date().toISOString().slice(0,10);
async function init(){
 if(!sql) return;
 await sql`CREATE TABLE IF NOT EXISTS students (student_id TEXT PRIMARY KEY,name TEXT NOT NULL,roll_no TEXT,course TEXT,semester TEXT,section TEXT,parent_mobile TEXT,parent_email TEXT,face_descriptor JSONB,created_at TIMESTAMPTZ DEFAULT now())`;
 await sql`CREATE TABLE IF NOT EXISTS timetable (lecture_no INTEGER PRIMARY KEY,subject TEXT NOT NULL,start_time TEXT NOT NULL,end_time TEXT NOT NULL)`;
 await sql`CREATE TABLE IF NOT EXISTS attendance (student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,attendance_date DATE NOT NULL,lecture_no INTEGER NOT NULL,lecture_start TEXT,lecture_end TEXT,in_time TEXT,out_time TEXT,status TEXT,PRIMARY KEY(student_id,attendance_date,lecture_no))`;
 await sql`CREATE TABLE IF NOT EXISTS movement (id BIGSERIAL PRIMARY KEY,student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,attendance_date DATE NOT NULL,lecture_no INTEGER NOT NULL,movement_type TEXT NOT NULL,event_time TEXT NOT NULL,reason TEXT)`;
 await sql`CREATE TABLE IF NOT EXISTS user_accounts (username TEXT PRIMARY KEY,role TEXT NOT NULL,password_hash TEXT NOT NULL)`;
 await sql`CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY,username TEXT,role TEXT,action TEXT,details TEXT,event_time TIMESTAMPTZ DEFAULT now())`;
 await sql`CREATE TABLE IF NOT EXISTS holidays (holiday_date DATE PRIMARY KEY,name TEXT NOT NULL,holiday_type TEXT NOT NULL)`;
 await sql`CREATE TABLE IF NOT EXISTS notifications (id BIGSERIAL PRIMARY KEY,student_id TEXT,student_name TEXT,channel TEXT,recipient TEXT,subject TEXT,message TEXT,status TEXT,details TEXT,sent_at TIMESTAMPTZ DEFAULT now())`;
 await sql`CREATE TABLE IF NOT EXISTS notification_settings (key TEXT PRIMARY KEY,value TEXT)`;
 const n=await sql`SELECT count(*)::int c FROM timetable`; if(!n[0].c) for(const r of DEFAULT_LECTURES) await sql`INSERT INTO timetable VALUES(${r[0]},${r[1]},${r[2]},${r[3]})`;
 const u=await sql`SELECT count(*)::int c FROM user_accounts`; if(!u[0].c){for(const r of [['admin','Admin'],['teacher','Teacher'],['viewer','Viewer']]) await sql`INSERT INTO user_accounts(username,role,password_hash) VALUES(${r[0]},${r[1]},${hash('1234')})`;}
}
function noDb(res){return res.status(503).json({ok:false,error:'DATABASE_URL is not configured. Set it in Vercel project settings.'})}
export default async function handler(req,res){
 if(!sql) return noDb(res); await init();
 const u=new URL(req.url,'http://localhost'); const r=u.searchParams.get('resource')||'bootstrap';
 try{
  if(r==='health') return res.json({ok:true,database:true,time:new Date().toISOString()});
  if(r==='bootstrap'){
   const [students,timetable,attendance,movement,holidays,notifications,users]=await Promise.all([
    sql`SELECT student_id,name,roll_no,course,semester,section,parent_mobile,parent_email,face_descriptor FROM students ORDER BY student_id`,
    sql`SELECT lecture_no,subject,start_time,end_time FROM timetable ORDER BY lecture_no`,
    sql`SELECT * FROM attendance ORDER BY attendance_date DESC,lecture_no,student_id`,
    sql`SELECT * FROM movement ORDER BY attendance_date DESC,event_time DESC`,
    sql`SELECT holiday_date,name,holiday_type FROM holidays ORDER BY holiday_date`,
    sql`SELECT * FROM notifications ORDER BY sent_at DESC LIMIT 500`,
    sql`SELECT username,role FROM user_accounts ORDER BY username`
   ]); return res.json({ok:true,students,timetable,attendance,movement,holidays,notifications,users});
  }
  if(r==='login'&&req.method==='POST'){
   const {username,password}=req.body||{}; const rows=await sql`SELECT username,role,password_hash FROM user_accounts WHERE username=${username}`;
   if(!rows.length||rows[0].password_hash!==hash(password)) return res.status(401).json({ok:false,error:'Invalid username or password'});
   await sql`INSERT INTO audit_log(username,role,action,details) VALUES(${rows[0].username},${rows[0].role},'LOGIN','Web login')`;
   return res.json({ok:true,username:rows[0].username,role:rows[0].role});
  }
  if(r==='students'&&req.method==='POST'){
   const s=req.body||{}; await sql`INSERT INTO students(student_id,name,roll_no,course,semester,section,parent_mobile,parent_email,face_descriptor) VALUES(${s.student_id},${s.name},${s.roll_no||''},${s.course||''},${s.semester||''},${s.section||''},${s.parent_mobile||''},${s.parent_email||''},${s.face_descriptor?JSON.stringify(s.face_descriptor):null}) ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,roll_no=excluded.roll_no,course=excluded.course,semester=excluded.semester,section=excluded.section,parent_mobile=excluded.parent_mobile,parent_email=excluded.parent_email,face_descriptor=excluded.face_descriptor`;
   return res.json({ok:true});
  }
  if(r==='students'&&req.method==='DELETE'){await sql`DELETE FROM students WHERE student_id=${u.searchParams.get('id')}`;return res.json({ok:true});}
  if(r==='attendance'&&req.method==='POST'){
   const a=req.body||{}; const d=a.attendance_date||today();
   if(a.action==='in') await sql`INSERT INTO attendance(student_id,attendance_date,lecture_no,lecture_start,lecture_end,in_time,out_time,status) VALUES(${a.student_id},${d},${a.lecture_no},${a.lecture_start},${a.lecture_end},${a.in_time},'',${a.status}) ON CONFLICT(student_id,attendance_date,lecture_no) DO NOTHING`;
   if(a.action==='final_out') await sql`UPDATE attendance SET out_time=${a.lecture_end} WHERE student_id=${a.student_id} AND attendance_date=${d} AND lecture_no=${a.lecture_no} AND coalesce(in_time,'')<>'' AND coalesce(out_time,'')=''`;
   return res.json({ok:true});
  }
  if(r==='movement'&&req.method==='POST'){const m=req.body||{}; await sql`INSERT INTO movement(student_id,attendance_date,lecture_no,movement_type,event_time,reason) VALUES(${m.student_id},${m.attendance_date||today()},${m.lecture_no},${m.movement_type},${m.event_time},${m.reason||''})`;return res.json({ok:true});}
  if(r==='holiday'&&req.method==='POST'){const h=req.body||{};await sql`INSERT INTO holidays(holiday_date,name,holiday_type) VALUES(${h.holiday_date},${h.name},${h.holiday_type||'College Custom'}) ON CONFLICT(holiday_date) DO UPDATE SET name=excluded.name,holiday_type=excluded.holiday_type`;return res.json({ok:true});}
  if(r==='holiday'&&req.method==='DELETE'){await sql`DELETE FROM holidays WHERE holiday_date=${u.searchParams.get('date')} AND holiday_type='College Custom'`;return res.json({ok:true});}
  if(r==='timetable'&&req.method==='POST'){const t=req.body||{};await sql`INSERT INTO timetable(lecture_no,subject,start_time,end_time) VALUES(${t.lecture_no},${t.subject},${t.start_time},${t.end_time}) ON CONFLICT(lecture_no) DO UPDATE SET subject=excluded.subject,start_time=excluded.start_time,end_time=excluded.end_time`;return res.json({ok:true});}
  if(r==='notifications'&&req.method==='POST'){const n=req.body||{};await sql`INSERT INTO notifications(student_id,student_name,channel,recipient,subject,message,status,details) VALUES(${n.student_id},${n.student_name},${n.channel},${n.recipient||''},${n.subject||''},${n.message||''},${n.status||'Queued'},${n.details||''})`;return res.json({ok:true});}
  if(r==='audit'&&req.method==='POST'){const a=req.body||{};await sql`INSERT INTO audit_log(username,role,action,details) VALUES(${a.username},${a.role},${a.action},${a.details||''})`;return res.json({ok:true});}
  return res.status(404).json({ok:false,error:'Unknown resource'});
 }catch(e){console.error(e);return res.status(500).json({ok:false,error:e.message});}
}
