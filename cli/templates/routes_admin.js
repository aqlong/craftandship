const express = require('express');
const router = express.Router();
const bcrypt = require('bcryptjs');
const multer = require('multer');
const csvParser = require('csv-parser');
const { Readable } = require('stream');
const pool = require('../db');
const { requireAuth, validateCsrfToken } = require('../middleware/auth');

// Admin guard: only the first registered user (id=1) is admin
function requireAdmin(req, res, next) {
  if (!req.session.userId) return res.redirect('/login');
  if (req.session.userRole !== 'admin' && req.session.userId !== 1) {
    return res.status(403).render('403', { title: 'Forbidden' });
  }
  next();
}

const upload = multer({ limits: { fileSize: 5 * 1024 * 1024 }, fileFilter: (req, file, cb) => {
  if (file.mimetype === 'text/csv' || file.originalname.endsWith('.csv')) cb(null, true);
  else cb(new Error('Only CSV files are allowed'));
}});

// GET /admin/csv-import: CSV import instructions page
router.get('/admin/csv-import', requireAdmin, async (req, res) => {
  try {
    const result = await pool.query(
      `SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename NOT IN ('users', 'session')`
    );
    const tables = result.rows.map(r => r.tablename).sort();
    const flash = req.session.flash || '';
    delete req.session.flash;
    res.render('admin-csv-import', {
      title: 'CSV Import',
      tables,
      flash,
      csrfToken: res.locals.csrfToken || ''
    });
  } catch (err) {
    console.error('CSV import page error:', err);
    res.status(500).render('500', { title: 'Error' });
  }
});

// GET /admin/csv-template/:table, download CSV template with example data
router.get('/admin/csv-template/:table', requireAdmin, async (req, res) => {
  const table = (req.params.table || '').replace(/[^a-z0-9_]/gi, '');
  if (!table) return res.status(400).send('Missing table name');
  try {
    const colResult = await pool.query(
      `SELECT column_name, data_type FROM information_schema.columns WHERE table_name = $1 AND column_name NOT IN ('id', 'created_at', 'updated_at', 'created_by') ORDER BY ordinal_position`,
      [table]
    );
    if (colResult.rows.length === 0) return res.status(404).send('Table not found');

    const cols = colResult.rows;
    const headers = cols.map(c => c.column_name);

    function exampleValue(colName, dataType, rowIdx) {
      const name = colName.toLowerCase();
      const samples = {
        name: ['Acme Corp', 'Sunrise Bakery', 'Coastal Designs'],
        title: ['Quarterly Review', 'Project Alpha Launch', 'Team Standup Notes'],
        email: ['contact@example.com', 'hello@acmecorp.example', 'info@sunrise.example'],
        phone: ['555-0123', '555-0456', '555-0789'],
        address: ['123 Main St, Springfield', '456 Oak Ave, Portland', '789 Pine Rd, Austin'],
        city: ['Springfield', 'Portland', 'Austin'],
        state: ['IL', 'OR', 'TX'],
        zip: ['62701', '97201', '73301'],
        country: ['US', 'US', 'CA'],
        url: ['https://example.com', 'https://acmecorp.example', 'https://sunrise.example'],
        website: ['https://example.com', 'https://acmecorp.example', 'https://sunrise.example'],
        description: ['High-quality service for businesses', 'Fresh baked goods daily', 'Modern web design agency'],
        notes: ['Follow up next week', 'Preferred customer', 'Requires approval'],
        status: ['active', 'pending', 'completed'],
        type: ['standard', 'premium', 'basic'],
        category: ['General', 'Marketing', 'Operations'],
        priority: ['high', 'medium', 'low'],
        color: ['blue', 'red', 'green'],
        quantity: ['10', '25', '5'],
        amount: ['49.99', '150.00', '29.95'],
        price: ['49.99', '150.00', '29.95'],
        cost: ['25.00', '75.00', '15.00'],
        total: ['149.97', '450.00', '89.85'],
        rate: ['75.00', '125.00', '50.00'],
        percentage: ['15', '25', '10'],
        weight: ['2.5', '1.8', '3.2'],
        size: ['medium', 'large', 'small'],
        first_name: ['Alice', 'Bob', 'Carol'],
        last_name: ['Johnson', 'Smith', 'Williams'],
        company: ['Acme Corp', 'Sunrise Bakery', 'Coastal Designs'],
        role: ['manager', 'developer', 'designer'],
        username: ['alice_j', 'bob_smith', 'carol_w'],
        password: ['password123', 'password123', 'password123'],
        subject: ['Meeting follow-up', 'Invoice #1042', 'Welcome aboard'],
        message: ['Looking forward to our next meeting', 'Please review the attached', 'Thanks for joining the team'],
        label: ['Important', 'Urgent', 'Review'],
        tag: ['featured', 'new', 'sale'],
        code: ['ABC-001', 'DEF-002', 'GHI-003'],
        sku: ['SKU-1001', 'SKU-1002', 'SKU-1003'],
        order_number: ['ORD-2026-001', 'ORD-2026-002', 'ORD-2026-003'],
        invoice_number: ['INV-2026-001', 'INV-2026-002', 'INV-2026-003'],
      };

      // Exact column name match
      if (samples[name]) return samples[name][rowIdx] || samples[name][0];

      // Partial match
      for (const [key, vals] of Object.entries(samples)) {
        if (name.includes(key)) return vals[rowIdx] || vals[0];
      }

      // Type-based fallback
      const dt = (dataType || '').toLowerCase();
      if (dt.includes('bool')) return rowIdx === 1 ? 'false' : 'true';
      if (dt.includes('int') || dt.includes('serial')) return String((rowIdx + 1) * 10);
      if (dt.includes('numeric') || dt.includes('decimal') || dt.includes('float') || dt.includes('double') || dt.includes('real') || dt.includes('money'))
        return ['49.99', '150.00', '29.95'][rowIdx] || '49.99';
      if (dt.includes('date') || dt.includes('timestamp'))
        return ['2026-01-15', '2026-02-20', '2026-03-10'][rowIdx] || '2026-01-15';
      if (dt.includes('json'))
        return ['{}', '{}', '{}'][rowIdx];
      if (dt.includes('uuid'))
        return '';

      // Generic text fallback
      return ['Sample value 1', 'Sample value 2', 'Sample value 3'][rowIdx] || 'Sample value';
    }

    let csv = headers.join(',') + '\n';
    for (let r = 0; r < 3; r++) {
      const row = cols.map(c => {
        const val = exampleValue(c.column_name, c.data_type, r);
        // Quote values containing commas or quotes
        if (val.includes(',') || val.includes('"') || val.includes('\n')) {
          return '"' + val.replace(/"/g, '""') + '"';
        }
        return val;
      });
      csv += row.join(',') + '\n';
    }

    res.setHeader('Content-Type', 'text/csv');
    res.setHeader('Content-Disposition', `attachment; filename="${table}_template.csv"`);
    res.send(csv);
  } catch (err) {
    console.error('CSV template error:', err);
    res.status(500).send('Error generating template');
  }
});

// POST /admin/reset-data: truncate all tables except users
router.post('/admin/reset-data', requireAdmin, validateCsrfToken, async (req, res) => {
  try {
    const tables = await pool.query(
      `SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename NOT IN ('users', 'session')`
    );
    for (const row of tables.rows) {
      await pool.query(`TRUNCATE TABLE "${row.tablename}" CASCADE`);
    }
    req.session.flash = 'All data has been cleared.';
    req.session.save(() => res.redirect('/dashboard'));
  } catch (err) {
    console.error('Reset data error:', err);
    res.status(500).render('500', { title: 'Error' });
  }
});

// POST /admin/csv-import: bulk import from CSV
router.post('/admin/csv-import', requireAdmin, validateCsrfToken, upload.single('file'), async (req, res) => {
  const table = (req.query.table || req.body.table || '').replace(/[^a-z0-9_]/gi, '');
  if (!table || !req.file) {
    req.session.flash = 'Missing table name or file.';
    return req.session.save(() => res.redirect('back'));
  }
  try {
    const rows = [];
    const stream = Readable.from(req.file.buffer);
    await new Promise((resolve, reject) => {
      stream.pipe(csvParser())
        .on('data', (row) => rows.push(row))
        .on('end', resolve)
        .on('error', reject);
    });
    if (rows.length === 0) {
      req.session.flash = 'CSV file is empty.';
      return req.session.save(() => res.redirect('back'));
    }
    // Get valid column names for the table
    const colResult = await pool.query(
      `SELECT column_name FROM information_schema.columns WHERE table_name = $1 AND column_name NOT IN ('id', 'created_at', 'updated_at', 'created_by')`,
      [table]
    );
    const validCols = colResult.rows.map(r => r.column_name);
    let imported = 0;
    for (const row of rows) {
      const cols = [];
      const vals = [];
      const params = [];
      let i = 1;
      for (const [key, val] of Object.entries(row)) {
        const col = key.toLowerCase().trim();
        if (validCols.includes(col)) {
          cols.push(`"${col}"`);
          vals.push(`$${i++}`);
          params.push(val);
        }
      }
      if (cols.length > 0) {
        await pool.query(`INSERT INTO "${table}" (${cols.join(',')}) VALUES (${vals.join(',')})`, params);
        imported++;
      }
    }
    req.session.flash = `Imported ${imported} row(s) into ${table}.`;
    req.session.save(() => res.redirect('back'));
  } catch (err) {
    console.error('CSV import error:', err);
    req.session.flash = `Import failed: ${err.message}`;
    req.session.save(() => res.redirect('back'));
  }
});

// --- Logo upload ---
const logoUpload = multer({
  limits: { fileSize: 2 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    if (['image/png', 'image/jpeg', 'image/svg+xml', 'image/webp'].includes(file.mimetype)) cb(null, true);
    else cb(new Error('Only PNG, JPEG, SVG, or WebP images are allowed'));
  }
}).single('logo');

// POST /admin/logo: upload or replace the app logo
router.post('/admin/logo', requireAdmin, (req, res, next) => {
  logoUpload(req, res, (err) => {
    if (err) {
      req.session.flash = err.message || 'Upload failed';
      return req.session.save(() => res.redirect('back'));
    }
    next();
  });
}, validateCsrfToken, async (req, res) => {
  if (!req.file) {
    req.session.flash = 'No file selected.';
    return req.session.save(() => res.redirect('back'));
  }
  try {
    const fs = require('fs');
    const path = require('path');
    const logoPath = path.join(__dirname, '..', 'public', 'logo.png');
    fs.writeFileSync(logoPath, req.file.buffer);
    req.session.flash = 'Logo updated successfully.';
    req.session.save(() => res.redirect('/dashboard'));
  } catch (err) {
    console.error('Logo upload error:', err);
    res.status(500).render('500', { title: 'Error' });
  }
});

// POST /admin/change-password
router.post('/admin/change-password', requireAdmin, validateCsrfToken, async (req, res) => {
  const { current_password, new_password } = req.body;
  if (!current_password || !new_password) {
    req.session.flash = 'Both current and new password are required.';
    return req.session.save(() => res.redirect('/dashboard'));
  }
  if (new_password.length < 8) {
    req.session.flash = 'New password must be at least 8 characters.';
    return req.session.save(() => res.redirect('/dashboard'));
  }
  try {
    const user = await pool.query('SELECT password FROM users WHERE id = $1', [req.session.userId]);
    if (user.rows.length === 0) {
      req.session.flash = 'User not found.';
      return req.session.save(() => res.redirect('/dashboard'));
    }
    const valid = await bcrypt.compare(current_password, user.rows[0].password);
    if (!valid) {
      req.session.flash = 'Current password is incorrect.';
      return req.session.save(() => res.redirect('/dashboard'));
    }
    const hashed = await bcrypt.hash(new_password, 10);
    await pool.query('UPDATE users SET password = $1 WHERE id = $2', [hashed, req.session.userId]);
    req.session.flash = 'Password changed successfully.';
    req.session.save(() => res.redirect('/dashboard'));
  } catch (err) {
    console.error('Change password error:', err);
    res.status(500).render('500', { title: 'Error' });
  }
});

module.exports = router;
