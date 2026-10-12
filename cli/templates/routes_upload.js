/**
 * Image upload route - secure, authenticated file upload with auto-resize.
 * Injected by _ensure_upload_route() when spec has image-related fields.
 */
const express = require('express');
const router = express.Router();
const multer = require('multer');
const path = require('path');
const crypto = require('crypto');
const { requireAuth } = require('../middleware/auth');
const pool = require('../db');

let sharp;
try { sharp = require('sharp'); } catch(e) { sharp = null; }

// 5MB file size limit
const MAX_FILE_SIZE = 5 * 1024 * 1024;

// Magic bytes for allowed image types
const MAGIC_BYTES = {
  'ffd8ff': 'image/jpeg',      // JPEG
  '89504e47': 'image/png',     // PNG
  '52494646': 'image/webp',    // WebP (RIFF header)
  '47494638': 'image/gif',     // GIF
};

// Multer config with memory storage
const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: MAX_FILE_SIZE },
  fileFilter: function(req, file, cb) {
    const allowedMimes = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
    if (allowedMimes.includes(file.mimetype)) {
      cb(null, true);
    } else {
      cb(new Error('Only JPEG, PNG, WebP, and GIF images are allowed'), false);
    }
  }
});

// Rate limiting: 10 uploads per minute per user
const uploadCounts = new Map();
function rateLimit(req, res, next) {
  const userId = req.session.userId;
  const now = Date.now();
  const windowMs = 60 * 1000;

  if (!uploadCounts.has(userId)) {
    uploadCounts.set(userId, []);
  }

  const timestamps = uploadCounts.get(userId).filter(t => now - t < windowMs);
  if (timestamps.length >= 10) {
    return res.status(429).json({ error: 'Upload limit exceeded. Try again in a minute.' });
  }

  timestamps.push(now);
  uploadCounts.set(userId, timestamps);
  next();
}

/**
 * Validate file content by checking magic bytes.
 */
function validateMagicBytes(buffer) {
  const hex = buffer.slice(0, 4).toString('hex');
  for (const [magic, mime] of Object.entries(MAGIC_BYTES)) {
    if (hex.startsWith(magic)) {
      return mime;
    }
  }
  return null;
}

/**
 * Process an image: auto-resize to max 1200px and convert to progressive JPEG.
 * Falls back to raw buffer if sharp is not available.
 */
async function processImage(buffer) {
  if (sharp) {
    const processed = await sharp(buffer)
      .resize(1200, 1200, { fit: 'inside', withoutEnlargement: true })
      .jpeg({ quality: 82, progressive: true })
      .toBuffer();
    return { buffer: processed, mimeType: 'image/jpeg' };
  }
  return { buffer, mimeType: null };
}

/**
 * POST /api/upload
 * Authenticated image upload endpoint with auto-resize.
 * Returns: { success: true, url: '/api/uploads/:id', filename: '...', size: N }
 */
router.post('/api/upload', requireAuth, rateLimit, upload.single('file'), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ error: 'No image file provided' });
    }

    // Validate magic bytes
    const detectedMime = validateMagicBytes(req.file.buffer);
    if (!detectedMime) {
      return res.status(415).json({ error: 'Invalid image file format' });
    }

    // Auto-resize with sharp (if available)
    const processed = await processImage(req.file.buffer);
    const finalBuffer = processed.buffer;
    const mimeType = processed.mimeType || detectedMime;

    // Generate UUID filename
    const ext = sharp ? '.jpg' : (path.extname(req.file.originalname) || '.jpg');
    const filename = crypto.randomUUID() + ext;

    // Store as base64 in database (survives Railway redeploys)
    const base64Data = finalBuffer.toString('base64');

    const result = await pool.query(
      `INSERT INTO uploads (id, filename, mime_type, data, size_bytes, uploaded_by, created_at)
       VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, NOW())
       RETURNING id, filename`,
      [filename, mimeType, base64Data, finalBuffer.length, req.session.userId]
    );

    const upload_record = result.rows[0];
    res.json({
      success: true,
      url: '/api/uploads/' + upload_record.id,
      filename: upload_record.filename,
      size: finalBuffer.length,
    });
  } catch (err) {
    console.error('Upload error:', err);
    res.status(500).json({ error: 'Upload failed' });
  }
});

/**
 * GET /api/uploads/:id
 * Serve an uploaded image by ID.
 */
router.get('/api/uploads/:id', async (req, res) => {
  try {
    const result = await pool.query(
      'SELECT filename, mime_type, data FROM uploads WHERE id = $1',
      [req.params.id]
    );
    if (!result.rows.length) {
      return res.status(404).json({ error: 'Image not found' });
    }
    const img = result.rows[0];
    const buffer = Buffer.from(img.data, 'base64');
    res.set('Content-Type', img.mime_type);
    res.set('Cache-Control', 'public, max-age=86400');
    res.send(buffer);
  } catch (err) {
    console.error('Image serve error:', err);
    res.status(500).json({ error: 'Failed to serve image' });
  }
});

module.exports = router;
