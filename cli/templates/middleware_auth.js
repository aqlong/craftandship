const crypto = require('crypto');

function requireAuth(req, res, next) {
  if (!req.session.userId) {
    return res.redirect('/login');
  }
  next();
}

function requireRole(...roles) {
  return (req, res, next) => {
    if (!req.session.userId) {
      return res.redirect('/login');
    }
    if (!roles.includes(req.session.userRole)) {
      return res.status(403).render('403', { title: 'Forbidden' });
    }
    next();
  };
}

function generateCsrfToken(req, res, next) {
  if (!req.session.csrfToken) {
    req.session.csrfToken = crypto.randomBytes(32).toString('hex');
  }
  res.locals.csrfToken = req.session.csrfToken;
  next();
}

function validateCsrfToken(req, res, next) {
  const token = req.body._csrf || req.headers['x-csrf-token'];
  if (!token || token !== req.session.csrfToken) {
    return res.status(403).json({ error: 'Invalid or missing CSRF token.' });
  }
  next();
}

function requirePasswordSet(req, res, next) {
  if (req.session.userId && req.session.passwordSet === false) {
    const skipPaths = ['/set-password', '/logout', '/magic'];
    if (skipPaths.some(p => req.path.startsWith(p))) {
      return next();
    }
    return res.redirect('/set-password');
  }
  next();
}

module.exports = { requireAuth, requireRole, generateCsrfToken, validateCsrfToken, requirePasswordSet };
