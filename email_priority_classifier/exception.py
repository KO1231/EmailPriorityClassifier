class EmailPriorityClassifierException(Exception):
    """Base exception class for Email Priority Classifier errors."""
    pass


class EmailPriorityClassifierGmailAPIException(EmailPriorityClassifierException):
    """Exception for Gmail API related errors."""
    pass


class EmailPriorityClassifierClassifyException(EmailPriorityClassifierException):
    """Exception for errors during email classification."""
    pass


class EmailPriorityClassifierOpenAIException(EmailPriorityClassifierClassifyException):
    """Exception for OpenAI API related errors."""
    pass
